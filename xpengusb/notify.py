"""Optional phone notifications with a persistent cursor and cooldown."""
import json
import time
from urllib.parse import urlencode
from urllib.error import HTTPError, URLError
from urllib.request import HTTPRedirectHandler, Request, build_opener

from .dashboard_control import PUSHOVER, STATE, SYNC_STATUS, read_json, write_json

STATE_FILE = STATE / "pushover-status.json"
DELIVERY_RETRY_SECONDS = 900


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


def send(credentials, title, message):
    data = {"token": credentials["token"], "user": credentials["user"],
            "title": title, "message": message}
    if credentials.get("device"):
        data["device"] = credentials["device"]
    request = Request("https://api.pushover.net/1/messages.json", data=urlencode(data).encode())
    try:
        with build_opener(NoRedirect()).open(request, timeout=8) as response:
            if json.loads(response.read(16384)).get("status") != 1:
                raise ValueError("Pushover rejected the request.")
    except HTTPError as error:
        with error:
            detail = error.read(8192).decode("utf-8", "replace").lower()
        if error.code == 429:
            message = "Pushover rate limit or message quota reached. Try again later."
        elif error.code >= 500:
            message = "Pushover is temporarily unavailable."
        elif "token" in detail:
            message = "Pushover rejected the application token."
        elif "device" in detail:
            message = "Pushover rejected the device name. Check the device or leave it blank."
        elif "user" in detail:
            message = "Pushover rejected the user key."
        else:
            message = "Pushover rejected the notification. Check the token, user key and device."
        raise ValueError(message) from error
    except (URLError, TimeoutError) as error:
        raise ValueError("The Pi could not reach Pushover over HTTPS. Check internet access.") from error


def _record_events(state, status):
    events = state.setdefault("events", [])

    def queue(title, message):
        # Coalesce repeated events while internet access or Pushover is down.
        if not any(item[0] == title for item in events):
            events.append([title, message])

    count = status.get("archived", 0)
    pending = status.get("pending", 0)
    attempt = status.get("archive_attempt", 0)
    archive = status.get("archive")
    capture = status.get("capture")
    phase = status.get("phase", "idle")
    if not state.get("batch_active") and ((pending and phase == "uploading") or
                                          count > state["reported"]):
        queue("Backup started", f"Checking {pending} queued files for NAS backup." if pending
              else "XpengUSB started backing up new recordings.")
        state["batch_active"] = True

    checked = status.get("capture_checked") or status.get("cycle")
    if capture and checked != state.get("capture_checked"):
        state["capture_checked"] = checked
        if capture == "completed":
            if state.pop("capture_alerted", False):
                queue("Capture recovered", "The Pi can import recordings from the USB volume again.")
            state["capture_failures"] = 0
        else:
            failures = state.get("capture_failures", 0) + 1
            state["capture_failures"] = failures
            low_space = capture.startswith("Low storage") or "breach the recording reserve" in capture
            if (low_space or failures >= 2) and not state.get("capture_alerted"):
                queue("Capture needs attention", "Low storage is limiting imports. Newer clips may "
                      "still be on the car USB volume." if low_space else
                      "The Pi could not import new clips. It will recheck automatically. See the dashboard.")
                state["capture_alerted"] = True

    failure = status.get("archive_failure", 0)
    if failure and failure != state.get("seen_archive_failure"):
        state["seen_archive_failure"] = failure
        if not state.get("archive_incident"):
            state["archive_incident"] = failure
            reason = status.get("archive_failure_reason", "")
            detail = ("The NAS connection or transfer was interrupted." if "interrupted" in reason else
                      "Some recordings could not be verified." if "verified" in reason else
                      "A NAS check or transfer failed. See the dashboard for details.")
            queue("Backup needs attention", f"{detail} {pending} files are queued. The Pi will retry.")

    incident = state.get("archive_incident")
    if incident and attempt > incident and not state.get("archive_retry_announced"):
        queue("Backup retry", "The Pi is retrying the NAS backup after the failure.")
        state["archive_retry_announced"] = True

    if phase == "idle" and archive == "completed" and pending == 0:
        if count > state["reported"]:
            note = " Newer clips may still be on the USB volume because capture needs attention." if (
                capture and capture != "completed") else ""
            queue("Backup complete", f"{count - state['reported']} files verified on the NAS. "
                  f"The upload queue is empty.{note}")
            state["reported"] = count
        state["batch_active"] = False
        if incident and attempt > incident:
            state.pop("archive_incident", None)
            state.pop("archive_retry_announced", None)


def tick(client):
    if not client or not PUSHOVER.exists():
        return
    credentials = read_json(PUSHOVER, {})
    if not credentials or not credentials.get("enabled", True):
        return
    status = read_json(SYNC_STATUS, {})
    if not (status.get("cycle") or status.get("archive_attempt")):
        return
    previous = read_json(STATE_FILE, {})
    state = dict(previous)
    state["events"] = list(previous.get("events", []))
    state.setdefault("reported", status.get("archived", 0))
    if "events" not in previous:
        # The old single cooldown suppressed unrelated messages for 15 minutes.
        state["retry_after"] = 0
    destination = status.get("destination")
    if state.get("destination", destination) != destination:
        state = {"reported": status.get("archived", 0), "events": [], "retry_after": 0}
    state["destination"] = destination
    _record_events(state, status)
    if state != previous:
        write_json(STATE_FILE, state)
    now = time.time()
    if not state["events"] or now < state.get("retry_after", 0):
        return
    # Persist the cooldown before contacting Pushover, including on a crash.
    state["retry_after"] = now + DELIVERY_RETRY_SECONDS
    write_json(STATE_FILE, state)
    title, message = state["events"][0]
    try:
        send(credentials, "XpengUSB " + title.lower(), message)
    except (OSError, ValueError):
        print("Pushover delivery failed. Check credentials, device and internet access.", flush=True)
        return
    state["events"].pop(0)
    state["retry_after"] = 0
    write_json(STATE_FILE, state)
