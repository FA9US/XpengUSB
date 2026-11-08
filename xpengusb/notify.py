"""Optional phone notifications with a persistent cursor and cooldown."""
import json
import time
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .dashboard_control import PUSHOVER, STATE, SYNC_STATUS, read_json, write_json

STATE_FILE = STATE / "pushover-status.json"


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(credentials, title, message):
    data = {"token": credentials["token"], "user": credentials["user"],
            "title": title, "message": message}
    if credentials.get("device"):
        data["device"] = credentials["device"]
    request = Request("https://api.pushover.net/1/messages.json", data=urlencode(data).encode())
    with build_opener(NoRedirect()).open(request, timeout=8) as response:
        if json.loads(response.read(16384)).get("status") != 1:
            raise ValueError("Pushover rejected the request.")


def tick(client):
    if not client or not PUSHOVER.exists():
        return
    status = read_json(SYNC_STATUS, {})
    if not status.get("cycle"):
        return
    count = status.get("archived", 0)
    now = time.time()
    state = read_json(STATE_FILE, {"reported": count, "cycle": 0, "failures": 0,
                                   "alerted": False, "retry_after": 0})
    if state["cycle"] != status["cycle"]:
        state["cycle"] = status["cycle"]
        failed = status.get("capture") != "completed" or status.get("archive") not in {
            "completed", "waiting for synchronized clock"}
        state["failures"] = state["failures"] + 1 if failed else 0
        if not failed:
            state["alerted"] = False
        write_json(STATE_FILE, state)
    if now < state["retry_after"]:
        return
    alert = state["failures"] >= 3 and not state["alerted"]
    summary = (status.get("archive") == "completed" and status.get("pending") == 0
               and count > state["reported"])
    if not alert and not summary:
        return
    title = "XpengUSB needs attention" if alert else "XpengUSB backup complete"
    message = ("Several checks failed. Open the dashboard to check storage and NAS status." if alert
               else f"{count - state['reported']} files verified on the NAS. The upload queue is empty.")
    # Save retry timing first to avoid hammering the service after abrupt shutdown.
    state["retry_after"] = now + 900
    write_json(STATE_FILE, state)
    try:
        send(read_json(PUSHOVER), title, message)
    except (OSError, ValueError):
        print("Pushover delivery failed. Check credentials, device and internet access.", flush=True)
        return
    if alert:
        state["alerted"] = True
    else:
        state["reported"] = count
    write_json(STATE_FILE, state)
