"""Recorder, backup and configuration status."""
from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.util import dt as dt_util

from .entity import PiEntity

SENSORS = {
    "os_disk": "Pi system disk free",
    "storage_disk": "Pi archive disk free",
    "usb_disk": "USB free at last snapshot",
    "recordings": "Recordings in last snapshot",
    "pending": "Pending NAS files",
    "archived": "Verified NAS files",
    "last_success": "Last successful NAS check",
    "last_upload": "Last verified upload",
    "sync": "Sync status",
    "network": "Network mode",
    "connected_ssid": "Connected Wi-Fi",
    "home_ssid": "Home Wi-Fi",
    "hotspot_ssid": "Hotspot SSID",
    "nas_destination": "NAS destination",
    "last_action": "Last control result",
}
DISKS = {"os_disk", "storage_disk", "usb_disk"}
TIMESTAMPS = {"last_success", "last_upload"}


async def async_setup_entry(hass, entry, async_add_entities):
    async_add_entities(PiSensor(entry.runtime_data, key, name) for key, name in SENSORS.items())


class PiSensor(PiEntity, SensorEntity):
    def __init__(self, coordinator, key, name):
        super().__init__(coordinator, key, name)
        self.key = key
        if key in DISKS:
            self._attr_device_class = SensorDeviceClass.DATA_SIZE
            self._attr_native_unit_of_measurement = "GiB"
        elif key in TIMESTAMPS:
            self._attr_device_class = SensorDeviceClass.TIMESTAMP

    @property
    def native_value(self):
        value = (self.coordinator.data or {}).get(self.key)
        if self.key in DISKS:
            return round(value["free"] / 2**30, 2) if value else None
        if self.key in TIMESTAMPS:
            return dt_util.utc_from_timestamp(value) if value else None
        return value[:255] if isinstance(value, str) else value

    @property
    def extra_state_attributes(self):
        if self.key in DISKS:
            value = (self.coordinator.data or {}).get(self.key)
            if value:
                return {"total_gib": round(value["total"] / 2**30, 2)}
        return None
