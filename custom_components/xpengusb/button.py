"""Recorder actions use the saved Pi settings."""
from homeassistant.components.button import ButtonEntity

from .entity import PiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([
        PiButton(entry.runtime_data, "sync", "Sync now"),
        PiButton(entry.runtime_data, "test-nas", "Test NAS"),
        PiButton(entry.runtime_data, "test-pushover", "Test Pushover"),
    ])


class PiButton(PiEntity, ButtonEntity):
    def __init__(self, coordinator, action, name):
        super().__init__(coordinator, action, name)
        self.action = action

    async def async_press(self):
        await self.coordinator.action("ha-" + self.action)
