"""Set up the local XpengUSB device."""
from datetime import timedelta
import logging
import time

from homeassistant.const import STATE_ON
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import PiClient, PiError, PiAuthError
from .arrival import Arrival

DOMAIN = "xpengusb"
PLATFORMS = ["sensor", "binary_sensor", "switch", "button"]
_LOGGER = logging.getLogger(__name__)


class Coordinator(DataUpdateCoordinator):
    def __init__(self, hass, entry):
        super().__init__(hass, _LOGGER, name="XpengUSB", update_interval=timedelta(minutes=1))
        self.client = PiClient(async_get_clientsession(hass), entry.data["url"],
                               entry.data["token"], entry.data["fingerprint"])
        self.arrival = Arrival()
        self.parked_entity = entry.data.get("parked_entity", "")
        self.alert = entry.data.get("arrival_alert", True)
        self.device_id = entry.unique_id
        self.last_action = None

    async def action(self, action):
        try:
            result = await self.client.request(action)
        except PiError as error:
            raise HomeAssistantError(str(error)) from error
        if result.get("message") and self.data:
            self.last_action = result["message"]
            self.async_set_updated_data({**self.data, "last_action": self.last_action})

    async def _async_update_data(self):
        try:
            data = await self.client.status()
            if data["id"] != self.device_id:
                raise PiError("This address belongs to a different XpengUSB recorder.")
        except PiAuthError as error:
            self.arrival.since = None
            raise UpdateFailed(str(error)) from error
        except PiError as error:
            self.arrival.disconnected()
            raise UpdateFailed(str(error)) from error
        data["last_action"] = self.last_action
        parked = self.hass.states.get(self.parked_entity) if self.parked_entity else None
        home = (data.get("network") == "Wi-Fi client" and bool(data.get("home_ssid"))
                and data.get("connected_ssid") == data["home_ssid"])
        confirmed = not self.parked_entity or bool(parked and parked.state == STATE_ON)
        now = time.monotonic()
        if self.arrival.ready(now, home and confirmed):
            if self.alert and data.get("pushover_enabled"):
                try:
                    await self.client.request("ha-arrival")
                except PiError as error:
                    self.arrival.retry(now)
                    _LOGGER.warning("XpengUSB arrival notification deferred: %s", error)
                    return data
            self.arrival.sent()
            self.hass.bus.async_fire("xpengusb_arrived", {"device": self.device_id,
                                                         "parked_sensor_confirmed": bool(parked and confirmed)})
        return data


async def async_setup_entry(hass, entry):
    coordinator = Coordinator(hass, entry)
    entry.runtime_data = coordinator
    await coordinator.async_refresh()
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass, entry):
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
