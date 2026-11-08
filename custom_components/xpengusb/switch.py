"""Enable or pause automatic Pushover messages."""
from homeassistant.components.switch import SwitchEntity

from .entity import PiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([PushoverSwitch(entry.runtime_data)])


class PushoverSwitch(PiEntity, SwitchEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, "pushover", "Pushover alerts")

    @property
    def is_on(self):
        return bool((self.coordinator.data or {}).get("pushover_enabled"))

    async def async_turn_on(self, **kwargs):
        await self.coordinator.action("ha-pushover-on")
        await self.coordinator.async_request_refresh()

    async def async_turn_off(self, **kwargs):
        await self.coordinator.action("ha-pushover-off")
        await self.coordinator.async_request_refresh()
