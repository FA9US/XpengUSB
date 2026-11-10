"""HTTPS dashboard. All privileged operations go through the local broker."""
import base64
import hashlib
import hmac
import html
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
import json
from pathlib import Path
import secrets
import socket
import ssl
import time
from urllib.parse import parse_qs

WEB = Path("/etc/xpengusb-web")
SOCKET = "/run/xpengusb-dashboard.sock"
CSRF = secrets.token_urlsafe(32)
AUTH = b""
HA_AUTH = b""
HA_POSTS = {"ha-test-nas", "ha-test-pushover", "ha-sync", "ha-pushover-on", "ha-pushover-off", "ha-arrival"}


def broker(action, fields):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(120)
        connection.connect(SOCKET)
        connection.sendall(json.dumps({"action": action, "fields": fields}).encode() + b"\n")
        with connection.makefile("rb") as stream:
            result = json.loads(stream.readline(32769))
    if "error" in result:
        raise ValueError(result["error"])
    return result["result"]


def escape(value):
    return html.escape(str(value), quote=True)


def field(name, label, value="", *, secret=False, hint=""):
    kind = "password" if secret else "text"
    return (f'<label>{escape(label)}<input name="{name}" type="{kind}" value="{escape(value)}" '
            f'placeholder="{escape(hint)}" maxlength="512" autocomplete="off"></label>')


def form(action, content, button, test_label=""):
    test = (f'<button class="secondary" formaction="/test-{action}">{escape(test_label)}</button>'
            if test_label else "")
    return (f'<form method="post" action="/{action}">'
            f'<input type="hidden" name="csrf" value="{CSRF}">{content}'
            f'<button>{escape(button)}</button>{test}</form>')


def card(title, content):
    return f"<section><h2>{escape(title)}</h2>{content}</section>"


def disk_text(value):
    if not value:
        return "Waiting for a measurement"
    return f'{value["free"] / 2**30:.1f} GiB free of {value["total"] / 2**30:.1f} GiB'


def when(timestamp):
    if not timestamp:
        return "No successful sync yet"
    return time.strftime("%Y-%m-%d %H:%M:%S %Z", time.localtime(timestamp))


def render(status, message=""):
    counts, sync, network, nas = (status[key] for key in ("counts", "sync", "network", "nas"))
    capture = sync.get("capture", "Waiting for a recording snapshot")
    archive = sync.get("archive", "Waiting for the archive worker")
    phase = {"capturing": "Checking recordings", "uploading": "Checking the NAS queue"}.get(
        sync.get("phase"), "Between checks")
    pending_bytes = counts.get("pending_bytes")
    result = card("Status", "<dl>" + "".join(
        f"<dt>{escape(label)}</dt><dd>{escape(value)}</dd>" for label, value in (
            ("Pi system disk", disk_text(status["os_disk"])),
            ("Pi archive storage", disk_text(status["storage_disk"])),
            ("USB volume, last snapshot", disk_text(sync.get("usb_disk"))),
            ("MP4 recordings in last snapshot", sync.get("recordings", "Waiting for a snapshot")),
            ("Queued files", counts.get("pending", "—")),
            ("Queued size", f"{pending_bytes / 2**30:.2f} GiB" if pending_bytes is not None else "—"),
            ("Verified NAS files", counts.get("archived", "—")),
            ("Worker activity", phase),
            ("Last capture", capture), ("Last sync result", archive),
            ("Local retention check", sync.get("retention", "Waiting for a NAS check")),
            ("Last successful NAS check", when(sync.get("last_success"))),
            ("Last verified upload", when(sync.get("last_upload"))),
            ("Network", (network.get("mode", "Starting") + " " + network.get("ssid", "")).strip()),
        )) + "</dl>")
    result += card("Home Wi-Fi",
                   form("home", field("ssid", "Network name", status["home_ssid"])
                        + field("password", "Wi-Fi password", secret=True), "Save and connect")
                   + form("retry", "", "Retry home Wi-Fi"))
    result += card("Fallback hotspot",
                   "<p>Changes can disconnect this browser. Reconnect using the new settings.</p>"
                   + form("hotspot", field("ssid", "Hotspot name", status["ap_ssid"])
                          + field("password", "Hotspot password", secret=True), "Save hotspot"))
    result += card("NAS sync", form("nas", field("server", "Server", nas["server"])
                   + field("share", "SMB share", nas["share"])
                   + field("directory", "Subfolder (optional)", nas["directory"], hint="cars/dashcam")
                   + field("username", "SMB username", nas["username"])
                   + field("domain", "Domain (optional)", nas["domain"])
                   + field("password", "SMB password", secret=True, hint="Leave blank to keep it"),
                   "Save NAS settings", "Test NAS"))
    enabled = status["pushover"]
    result += card("Pushover",
                   "<p>Get alerts when a backup starts, runs into a problem, retries, or clears the queue.</p>"
                   + form("pushover", '<label>Notifications<select name="enabled">'
                          + f'<option value="0"{" selected" if not enabled else ""}>Off</option>'
                          + f'<option value="1"{" selected" if enabled else ""}>On</option></select></label>'
                          + field("token", "Application token", secret=True, hint="Leave blank to keep it")
                          + field("user", "User key", secret=True, hint="Leave blank to keep it")
                          + field("device", "Phone device name (optional)", status.get("pushover_device", "")),
                          "Save notifications", "Send test notification"))
    if HA_AUTH:
        result += card("Home Assistant",
                       "<p><a href='/home-assistant'>Show private pairing details</a></p>")
    return ("<!doctype html><html lang=en><meta charset=utf-8>"
            "<meta name=viewport content='width=device-width,initial-scale=1'>"
            "<title>XpengUSB</title><style>"
            "body{font:16px system-ui;margin:0;background:#10161b;color:#edf5f3}"
            "main{max-width:980px;margin:auto;padding:1.4rem}"
            "header{display:flex;justify-content:space-between;align-items:center;gap:1rem}"
            "h1{color:#8cf0ca}h2{margin-top:0;font-size:1.2rem}a{color:#8cf0ca}"
            "article{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,300px),1fr));gap:1rem}"
            "section{padding:1.1rem;border:1px solid #35444b;border-radius:12px;background:#192329}"
            "section:first-child{grid-column:1/-1}dl{display:grid;grid-template-columns:1fr 1fr;gap:.55rem}"
            "dd{margin:0;overflow-wrap:anywhere}dt,p,label{color:#b6c8cb}"
            "label{display:block;margin:.65rem 0}input,select,button{font:inherit;border-radius:7px;padding:.55rem}"
            "input,select{box-sizing:border-box;width:100%;margin-top:.25rem;background:#10161b;"
            "color:#edf5f3;border:1px solid #647a80}button{background:#8cf0ca;color:#10161b;border:0;"
            "margin-top:.45rem;cursor:pointer}form+form{margin-top:.5rem}"
            "button.secondary{background:#35444b;color:#edf5f3;margin-left:.5rem}"
            ".notice{padding:.8rem;border:1px solid #8cf0ca;border-radius:7px}"
            "</style><main><header><h1>XpengUSB</h1><a href='/'>Refresh</a></header>"
            + (f'<p class="notice" role="status">{escape(message)}</p>' if message else "")
            + "<article>" + result + "</article></main></html>")


class Dashboard(BaseHTTPRequestHandler):
    server_version = "XpengUSB"
    sys_version = ""

    def log_message(self, *args):
        pass  # URLs, credentials and request details are deliberately excluded from logs.

    def respond(self, status, body, *, authenticate=False, content_type="text/html; charset=utf-8"):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; style-src 'unsafe-inline'; form-action 'self'; "
                         "frame-ancestors 'none'; base-uri 'none'")
        if authenticate:
            self.send_header("WWW-Authenticate", 'Basic realm="XpengUSB", charset="UTF-8"')
        self.end_headers()
        self.wfile.write(data)

    def authorized(self):
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if hmac.compare_digest(supplied, AUTH):
            return True
        self.respond(HTTPStatus.UNAUTHORIZED, "Sign in to XpengUSB.", authenticate=True)
        return False

    def show(self, message=""):
        try:
            page = render(broker("status", {}), message)
        except (OSError, ValueError, KeyError):
            self.respond(HTTPStatus.SERVICE_UNAVAILABLE, "Pi settings are temporarily unavailable.")
            return
        self.respond(HTTPStatus.OK, page)

    def do_GET(self):
        if self.path == "/api/v1/ha-status":
            self.api("ha-status")
            return
        if self.authorized():
            if self.path == "/":
                self.show()
            elif self.path == "/home-assistant" and HA_AUTH:
                der = ssl.PEM_cert_to_DER_cert((WEB / "cert.pem").read_text())
                fingerprint = hashlib.sha256(der).hexdigest()
                token = HA_AUTH.removeprefix(b"Bearer ").decode()
                self.respond(HTTPStatus.OK, "<!doctype html><html lang=en><meta charset=utf-8>"
                             "<meta name=viewport content='width=device-width,initial-scale=1'>"
                             "<title>XpengUSB pairing</title><style>"
                             "body{font:16px system-ui;margin:1.4rem;background:#10161b;color:#edf5f3}"
                             "code{overflow-wrap:anywhere}a{color:#8cf0ca}</style>"
                             "<h1>Home Assistant pairing</h1>"
                             f"<p>Token</p><code>{escape(token)}</code>"
                             f"<p>SHA-256 certificate fingerprint</p><code>{fingerprint}</code>"
                             "<p><a href='/'>Back to dashboard</a></p></html>")
            else:
                self.respond(HTTPStatus.NOT_FOUND, "Page not found.")

    def do_POST(self):
        if self.path.startswith("/api/v1/"):
            action = self.path.removeprefix("/api/v1/")
            if action in HA_POSTS:
                self.api(action)
            else:
                self.respond(HTTPStatus.NOT_FOUND, "Page not found.")
            return
        if not self.authorized():
            return
        if self.path not in ("/home", "/hotspot", "/retry", "/nas", "/pushover",
                             "/test-nas", "/test-pushover"):
            self.respond(HTTPStatus.NOT_FOUND, "Page not found.")
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if (not 0 < length <= 8192
                    or self.headers.get_content_type() != "application/x-www-form-urlencoded"):
                raise ValueError()
            pairs = parse_qs(self.rfile.read(length).decode("utf-8"), keep_blank_values=True,
                             strict_parsing=True, max_num_fields=10)
            if any(len(values) != 1 for values in pairs.values()):
                raise ValueError()
            token = pairs.pop("csrf", [""])[0]
            if not hmac.compare_digest(token.encode(), CSRF.encode()):
                self.respond(HTTPStatus.FORBIDDEN, "Refresh the dashboard before changing settings.")
                return
        except (ValueError, OSError):
            self.respond(HTTPStatus.BAD_REQUEST, "Invalid settings form.")
            return
        try:
            result = broker(self.path[1:], {key: value[0] for key, value in pairs.items()})
            message = result.get("message") or ("Saved. The next network check will apply the Wi-Fi change."
                       if self.path in ("/home", "/hotspot", "/retry") else "Settings saved.")
            self.show(message)
        except ValueError as error:
            self.show(str(error))
        except OSError:
            self.respond(HTTPStatus.SERVICE_UNAVAILABLE, "The settings service did not respond.")

    def api(self, action):
        supplied = self.headers.get("Authorization", "").encode("utf-8")
        if not HA_AUTH or not hmac.compare_digest(supplied, HA_AUTH):
            status, response = HTTPStatus.UNAUTHORIZED, {"error": "Invalid Home Assistant token."}
        else:
            try:
                response = broker(action, {})
                status = HTTPStatus.OK
            except ValueError as error:
                status, response = HTTPStatus.BAD_REQUEST, {"error": str(error)}
            except OSError:
                status, response = HTTPStatus.SERVICE_UNAVAILABLE, {"error": "Pi control service unavailable."}
        self.respond(status, json.dumps(response), content_type="application/json")


class SecureServer(HTTPServer):
    def get_request(self):
        connection, peer = super().get_request()
        connection.settimeout(8)
        try:
            return self.context.wrap_socket(connection, server_side=True), peer
        except (OSError, ssl.SSLError):
            connection.close()
            raise


def main():
    global AUTH, HA_AUTH
    password = (WEB / "password").read_text().strip()
    AUTH = b"Basic " + base64.b64encode(("admin:" + password).encode())
    token = WEB / "ha-token"
    if token.is_file():
        HA_AUTH = b"Bearer " + token.read_text().strip().encode()
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.load_cert_chain(WEB / "cert.pem", WEB / "key.pem")
    with SecureServer(("0.0.0.0", 8443), Dashboard) as server:
        server.context = context
        server.serve_forever()


if __name__ == "__main__":
    main()
