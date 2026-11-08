"""Availability and radio state."""
from homeassistant.components.binary_sensor import BinarySensorDeviceClass, BinarySensorEntity

from .entity import PiEntity


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities([Online(entry.runtime_data), Hotspot(entry.runtime_data)])


class Online(PiEntity, BinarySensorEntity):
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY

    def __init__(self, coordinator):
        super().__init__(coordinator, "online", "Online")

    @property
    def available(self):
        return True

    @property
    def is_on(self):
        return self.coordinator.last_update_success


class Hotspot(PiEntity, BinarySensorEntity):
    def __init__(self, coordinator):
        super().__init__(coordinator, "hotspot", "Hotspot active")

    @property
    def is_on(self):
        return (self.coordinator.data or {}).get("network") == "Hotspot"
