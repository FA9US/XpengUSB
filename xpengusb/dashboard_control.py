"""Private operations for the dashboard and the single-radio Wi-Fi fallback."""
import argparse
import getpass
import json
import os
from pathlib import Path
import re
import sqlite3
import subprocess
import sys
import time

from configure import (CONFIG_PATH, SMB_CREDENTIAL_FILES, atomic_private_write,
                       nas_source, validate_credential)
from .archive import ArchiveStore
from .runtime import Config, RuntimeSafetyError, read_mounts

SETTINGS = Path("/etc/xpengusb")
STATE = Path("/var/lib/xpengusb")
WIFI = SETTINGS / "wifi.json"
PUSHOVER = SETTINGS / "pushover.json"
WIFI_STATUS = STATE / "wifi-status.json"
SYNC_STATUS = STATE / "sync-status.json"
PROFILE_DIR = Path("/etc/NetworkManager/system-connections")
HOME = "53fcc391-fc3c-4242-8fd1-18f21f851d01"
AP = "53fcc391-fc3c-4242-8fd1-18f21f851d02"
IFACE = "wlan0"


def read_json(path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        if default is not None:
            return default
        raise


def write_json(path, value):
    atomic_private_write(path, json.dumps(value) + "\n")


def run(*args, timeout=15, check=True):
    result = subprocess.run(args, text=True, capture_output=True, timeout=timeout)
    if check and result.returncode:
        raise RuntimeError("The " + args[0] + " operation failed.")
    return result


def nm(*args, check=True, wait=8):
    return run("nmcli", "--wait", str(wait), *args, timeout=wait + 5, check=check)


def active_wifi():
    lines = nm("-t", "--escape", "no", "-f", "UUID,DEVICE", "connection", "show",
               "--active").stdout.splitlines()
    return next((line.split(":")[0] for line in lines if line.endswith(":" + IFACE)), "")


def wifi_ssid(profile):
    if not profile:
        return ""
    return nm("--escape", "no", "-g", "802-11-wireless.ssid", "connection", "show",
              "uuid", profile, check=False).stdout.rstrip("\n")


def wifi_values(ssid, password, *, allow_open=False):
    if (not 1 <= len(ssid.encode("utf-8")) <= 32 or ssid != ssid.strip()
            or not ssid.isprintable() or re.fullmatch(r"(\d+;)+", ssid)):
        raise ValueError("Use a printable Wi-Fi name of 1–32 bytes without outer spaces.")
    if allow_open and not password:
        return
    if (not password.isascii() or not password.isprintable()
            or not (8 <= len(password) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password))):
        raise ValueError("Use an 8–63 character WPA password or a 64-digit hexadecimal key.")


def profile_text(ssid, password, ap):
    wifi_values(ssid, password, allow_open=not ap)

    def escape(value):
        # GLib keyfiles use backslash escaping and strip unescaped leading spaces.
        return value.replace("\\", "\\\\").replace(" ", "\\s")

    uuid = AP if ap else HOME
    profile = (
        "[connection]\n"
        f"id=XpengUSB-{'hotspot' if ap else 'home'}\nuuid={uuid}\ntype=wifi\n"
        f"interface-name={IFACE}\nautoconnect={'false' if ap else 'true'}\n"
        "autoconnect-priority=100\nautoconnect-retries=2\n\n[wifi]\n"
        f"mode={'ap' if ap else 'infrastructure'}\nssid={escape(ssid)}\n"
    )
    if ap:
        profile += "band=bg\n"
    if password:
        profile += "\n[wifi-security]\nkey-mgmt=wpa-psk\npsk=" + escape(password) + "\n"
    if ap:
        return profile + "\n[ipv4]\nmethod=shared\naddress1=10.42.0.1/24\n\n[ipv6]\nmethod=disabled\n"
    return profile + "\n[ipv4]\nmethod=auto\n\n[ipv6]\nmethod=auto\n"


def set_profile(ssid, password, ap):
    path = PROFILE_DIR / ("xpengusb-hotspot.nmconnection" if ap else "xpengusb-home.nmconnection")
    old = path.read_text(encoding="utf-8") if path.exists() else None
    atomic_private_write(path, profile_text(ssid, password, ap))
    try:
        nm("connection", "load", str(path))
    except (RuntimeError, subprocess.TimeoutExpired):
        if old is None:
            path.unlink()
        else:
            atomic_private_write(path, old)
            nm("connection", "load", str(path), check=False)
        raise


def setup_wifi(password_stdin=False):
    if WIFI.exists():
        return
    current = active_wifi()
    if not current:
        raise ValueError("Connect the Pi to home Wi-Fi before installing the dashboard.")
    if password_stdin:
        password = sys.stdin.readline(129).rstrip("\r\n")
    else:
        password = getpass.getpass("Initial XpengUSB hotspot password (hidden): ")
    set_profile("XpengUSB", password, True)
    write_json(WIFI, {"home": current, "home_ssid": wifi_ssid(current),
                     "ap_ssid": "XpengUSB", "action": "", "last_retry": 0})


def network_tick():
    wifi = read_json(WIFI)
    current = active_wifi()
    now = time.time()
    action = wifi["action"]
    if action == "hotspot" and current == AP:
        nm("connection", "down", "uuid", AP, check=False)
        current = ""
    if action == "home" and wifi["home"]:
        wifi["last_retry"] = now
        nm("connection", "up", "uuid", wifi["home"], "ifname", IFACE, check=False, wait=25)
        current = active_wifi()
    if not current:
        nm("connection", "up", "uuid", AP, "ifname", IFACE, check=False, wait=20)
        current = active_wifi()
        wifi["last_retry"] = now
    elif current == AP and now - wifi["last_retry"] >= 180:
        peers = run("iw", "dev", IFACE, "station", "dump", check=False)
        if peers.returncode == 0 and not peers.stdout.strip() and wifi["home"]:
            wifi["last_retry"] = now
            nm("connection", "up", "uuid", wifi["home"], "ifname", IFACE, check=False, wait=25)
            current = active_wifi()
            if not current:
                nm("connection", "up", "uuid", AP, "ifname", IFACE, check=False, wait=20)
                current = active_wifi()
    wifi["action"] = ""
    if wifi != read_json(WIFI):
        write_json(WIFI, wifi)
    status = {"mode": "Hotspot" if current == AP else
              "Wi-Fi client" if current else "Disconnected", "ssid": wifi_ssid(current)}
    if status != read_json(WIFI_STATUS, {}):
        write_json(WIFI_STATUS, status)
    return bool(current and current != AP)


def nas_credentials(path):
    return dict(line.split("=", 1) for line in path.read_text(encoding="utf-8").splitlines()
                if "=" in line)


def nas_settings(fields):
    config = read_json(CONFIG_PATH)
    current = Config.from_dict(config)
    credentials = current.credentials_file
    if credentials not in SMB_CREDENTIAL_FILES:
        raise ValueError("Unexpected SMB credentials path.")
    previous = nas_credentials(credentials)
    config["nas_source"] = nas_source(fields["server"].strip(), fields["share"].strip())
    config["nas_directory"] = fields["directory"].strip()
    if not fields["password"] and (config["nas_source"] != current.nas_source
                                   or fields["username"].strip() != previous.get("username")
                                   or fields["domain"].strip() != previous.get("domain", "")):
        raise ValueError("Enter the SMB password when changing the share or account.")
    password = fields["password"] or previous.get("password", "")
    values = {"username": fields["username"].strip(), "password": password,
              "domain": fields["domain"].strip()}
    for key, value in values.items():
        validate_credential(value, key.capitalize(), allow_empty=(key == "domain"))
    try:
        Config.from_dict(config)
    except RuntimeSafetyError as error:
        raise ValueError(str(error)) from error
    return config, current, "".join(f"{k}={v}\n" for k, v in values.items() if v)


def saved_nas_fields():
    config = Config.from_dict(read_json(CONFIG_PATH))
    server, share = config.nas_source[2:].split("/", 1)
    credentials = nas_credentials(config.credentials_file)
    return {"server": server, "share": share, "directory": config.nas_directory,
            "username": credentials.get("username", ""), "domain": credentials.get("domain", ""),
            "password": ""}


def test_nas(fields):
    config, _, credentials = nas_settings(fields)
    request = dict(source=config["nas_source"], directory=config["nas_directory"],
                   credentials=credentials, device_id=config["archive_device_id"])
    try:
        result = subprocess.run(
            ["unshare", "--mount", "--propagation", "private", sys.executable, "-m", "xpengusb.nas_probe"],
            input=json.dumps(request), text=True, capture_output=True, timeout=38, check=False)
    except subprocess.TimeoutExpired as error:
        raise ValueError("The NAS test timed out. Check Wi-Fi, NAS availability and TCP port 445.") from error
    if result.returncode:
        raise ValueError("The Pi could not start the isolated SMB test.")
    response = json.loads(result.stdout)
    if "error" in response:
        raise ValueError(response["error"])
    return response


def set_nas(fields):
    old_config = CONFIG_PATH.read_text(encoding="utf-8")
    config, current, contents = nas_settings(fields)
    credentials = current.credentials_file
    replacement = next(path for path in SMB_CREDENTIAL_FILES if path != credentials)
    config["credentials_file"] = str(replacement)
    run("systemctl", "stop", "xpengusb.service", timeout=70)
    changed = False
    try:
        mounts = [entry for entry in read_mounts() if entry.target == current.nas_mount]
        if mounts and (len(mounts) != 1 or mounts[0].fstype != "cifs"
                       or mounts[0].source != current.nas_source or mounts[0].root != "/"):
            raise ValueError("The NAS mount is unexpected. Check it before changing settings.")
        if mounts:
            run("umount", "--", str(current.nas_mount), timeout=20)
        changed = True
        atomic_private_write(replacement, contents)
        write_json(CONFIG_PATH, config)
        run("systemctl", "start", "--no-block", "xpengusb.service")
    except Exception:
        if changed:
            atomic_private_write(CONFIG_PATH, old_config)
            replacement.unlink(missing_ok=True)
        run("systemctl", "start", "--no-block", "xpengusb.service", check=False)
        raise
    credentials.unlink(missing_ok=True)


def disk_usage(path):
    usage = os.statvfs(path)
    return {"total": usage.f_blocks * usage.f_frsize, "free": usage.f_bavail * usage.f_frsize}


def record_cycle(config, store, result):
    status = read_json(SYNC_STATUS, {})
    destination = config.nas_source + "/" + config.nas_directory
    if status.get("destination") != destination:
        status = {}
    now = time.time()
    counts = ArchiveStore.read_status(config.state_dir)
    status.update(cycle=time.time_ns(), destination=destination,
                  capture=result["capture"], archive=result["archive"],
                  archived=counts["archived"], pending=counts["pending"])
    if "usb_disk" in result:
        status["usb_disk"] = result["usb_disk"]
        status["recordings"] = store.db.execute(
            "SELECT COUNT(*) FROM candidates WHERE lower(relative) GLOB '*.mp4'"
        ).fetchone()[0]
    if result["archive"] == "completed":
        status["last_success"] = now
    if result.get("uploaded"):
        status["last_upload"] = now
    write_json(SYNC_STATUS, status)


def dashboard_status():
    config = Config.from_dict(read_json(CONFIG_PATH))
    wifi = read_json(WIFI)
    creds = nas_credentials(config.credentials_file)
    server, share = config.nas_source[2:].split("/", 1)
    mounted = any(m.target == config.storage_dir and m.fstype == "btrfs" for m in read_mounts())
    try:
        counts = ArchiveStore.read_status(config.state_dir) if mounted else {}
    except (OSError, RuntimeError, sqlite3.Error):
        counts = {}
    push = read_json(PUSHOVER, {})
    return {"os_disk": disk_usage("/"), "storage_disk": disk_usage(config.storage_dir) if mounted else {},
            "counts": counts, "sync": read_json(SYNC_STATUS, {}),
            "network": read_json(WIFI_STATUS, {}), "home_ssid": wifi["home_ssid"],
            "ap_ssid": wifi["ap_ssid"], "nas": {"server": server, "share": share,
            "directory": config.nas_directory,
            "username": creds.get("username", ""), "domain": creds.get("domain", "")},
            "pushover": bool(push and push.get("enabled", True)), "pushover_device": push.get("device", "")}


def push_settings(fields):
    old = read_json(PUSHOVER, {})
    token, user = (fields[key].strip() or old.get(key, "") for key in ("token", "user"))
    device = fields["device"].strip()
    if (not all(re.fullmatch(r"[A-Za-z0-9]{30}", value) for value in (token, user))
            or not re.fullmatch(r"[A-Za-z0-9_-]{0,25}", device)):
        raise ValueError("Check the Pushover application token, user key and optional device name.")
    return {"token": token, "user": user, "device": device, "enabled": fields["enabled"] == "1"}


def save_push(fields):
    if fields["enabled"] == "0" and not PUSHOVER.exists() and not (fields["token"] or fields["user"]):
        return
    write_json(PUSHOVER, push_settings(fields))


def send_push_test(settings):
    from .notify import send
    guard = Path("/run/xpengusb-push-test.json")
    now = time.monotonic()
    if now - read_json(guard, {"time": -60})["time"] < 10:
        raise ValueError("Wait 10 seconds before sending another test notification.")
    write_json(guard, {"time": now})
    send(settings, "XpengUSB test", "Your XpengUSB test notification reached Pushover.")
    return {"message": "Pushover accepted the test notification. Check your phone."}


def ha_status():
    config = Config.from_dict(read_json(CONFIG_PATH))
    wifi = read_json(WIFI)
    sync = read_json(SYNC_STATUS, {})
    network = read_json(WIFI_STATUS, {})
    push = read_json(PUSHOVER, {})
    # Return only status and non-secret settings. The snapshot and counts are
    # produced by the worker; a Home Assistant poll never scans recordings.
    return {"version": 1, "id": config.archive_device_id, "os_disk": disk_usage("/"),
            "storage_disk": disk_usage(config.storage_dir)
            if any(m.target == config.storage_dir and m.fstype == "btrfs" for m in read_mounts()) else {},
            "usb_disk": sync.get("usb_disk", {}),
            **{key: sync.get(key) for key in ("recordings", "pending", "archived", "last_success", "last_upload")},
            "sync": sync.get("archive", "Waiting for a cycle"), "network": network.get("mode", "Starting"),
            "connected_ssid": network.get("ssid", ""), "home_ssid": wifi["home_ssid"],
            "hotspot_ssid": wifi["ap_ssid"], "nas_destination": config.nas_source +
            ("/" + config.nas_directory if config.nas_directory else ""),
            "pushover_enabled": bool(push and push.get("enabled", True))}


def ha_action(action):
    if action == "ha-status":
        return ha_status()
    if action == "ha-test-nas":
        return test_nas(saved_nas_fields())
    if action == "ha-sync":
        if run("systemctl", "is-active", "--quiet", "xpengusb.service", check=False).returncode:
            raise ValueError("The archive worker is stopped.")
        Path("/run/xpengusb/sync-request").touch()
        return {"message": "Sync requested. The worker will finish its current cycle first."}
    push = read_json(PUSHOVER, {})
    if not push:
        raise ValueError("Save Pushover credentials in the Pi dashboard first.")
    if action == "ha-test-pushover":
        return send_push_test(push)
    if action == "ha-arrival":
        if not push.get("enabled", True):
            raise ValueError("Pushover alerts are disabled.")
        from .notify import send
        guard = Path("/run/xpengusb-arrival.json")
        now = time.monotonic()
        if now - read_json(guard, {"time": -900})["time"] < 600:
            return {"message": "A recent arrival notification was already accepted."}
        send(push, "XpengUSB back on home Wi-Fi",
             "The recorder has been connected to home Wi-Fi for five minutes.")
        write_json(guard, {"time": now})
        return {"message": "Pushover accepted the arrival notification."}
    push["enabled"] = action == "ha-pushover-on"
    write_json(PUSHOVER, push)
    return {}


FORMS = {
    "status": set(), "retry": set(), "home": {"ssid", "password"},
    "hotspot": {"ssid", "password"},
    "nas": {"server", "share", "directory", "username", "password", "domain"},
    "pushover": {"token", "user", "device", "enabled"},
}
FORMS.update({"test-nas": FORMS["nas"], "test-pushover": FORMS["pushover"]})
HA_ACTIONS = {"ha-status", "ha-test-nas", "ha-test-pushover", "ha-sync", "ha-pushover-on",
              "ha-pushover-off", "ha-arrival"}
FORMS.update({action: set() for action in HA_ACTIONS})


def dispatch(request):
    action, fields = request["action"], request["fields"]
    if (not isinstance(action, str) or action not in FORMS
            or not isinstance(fields, dict) or fields.keys() != FORMS[action]
            or any(not isinstance(value, str) or len(value) > 512 for value in fields.values())):
        raise ValueError("Invalid dashboard request.")
    if action == "status":
        return dashboard_status()
    if action in HA_ACTIONS:
        return ha_action(action)
    if action == "test-nas":
        return test_nas(fields)
    if action == "test-pushover":
        return send_push_test(push_settings(fields))
    if action == "nas":
        set_nas(fields)
    elif action == "pushover":
        save_push(fields)
    else:
        wifi = read_json(WIFI)
        if action in {"home", "hotspot"}:
            ssid, password = fields["ssid"], fields["password"]
            if not password:
                raise ValueError("Enter the WPA password when changing a Wi-Fi profile.")
            set_profile(ssid, password, action == "hotspot")
            if action == "home":
                wifi.update(home=HOME, home_ssid=ssid)
            else:
                wifi["ap_ssid"] = ssid
        if action == "retry" and not wifi["home"]:
            raise ValueError("Save your home Wi-Fi settings before retrying.")
        wifi["action"] = "hotspot" if action == "hotspot" else "home"
        write_json(WIFI, wifi)
    return {}


def main():
    import fcntl
    parser = argparse.ArgumentParser()
    parser.add_argument("mode", choices=("request", "network", "setup"))
    parser.add_argument("--password-stdin", action="store_true")
    args = parser.parse_args()
    if args.mode == "request":
        try:
            payload = sys.stdin.buffer.readline(8193)
            if len(payload) > 8192 or not payload.endswith(b"\n"):
                raise ValueError("Invalid dashboard request.")
            request = json.loads(payload)
            if isinstance(request, dict) and request.get("action") in ("status", "ha-status"):
                print(json.dumps({"result": dispatch(request)}), flush=True)
                return
        except (ValueError, OSError, KeyError, TypeError):
            print(json.dumps({"error": "Could not read dashboard settings."}), flush=True)
            return
    with open("/run/xpengusb-dashboard.lock", "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if args.mode == "setup":
            setup_wifi(args.password_stdin)
        elif args.mode == "network":
            client = network_tick()
            from .notify import tick
            tick(client)
        else:
            try:
                output = {"result": dispatch(request)}
            except (ValueError, KeyError, TypeError, AttributeError) as error:
                # Some parser errors can include user text. Only send explicitly safe messages.
                output = {"error": str(error) if type(error) is ValueError else "Invalid settings."}
            except Exception:
                output = {"error": "The operation could not finish. Check the Pi service log."}
            print(json.dumps(output), flush=True)


if __name__ == "__main__":
    main()
