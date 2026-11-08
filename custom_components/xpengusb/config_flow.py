"""Add one Pi with a scoped token and pinned TLS fingerprint."""
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    EntitySelector, EntitySelectorConfig, TextSelector, TextSelectorConfig, TextSelectorType,
)

from . import DOMAIN
from .api import PiAuthError, PiClient, PiError

SCHEMA = vol.Schema({
    vol.Required("url", default="https://xpengusb.local:8443"): str,
    vol.Required("token"): TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD)),
    vol.Required("fingerprint"): str,
    vol.Optional("arrival_alert", default=True): bool,
    vol.Optional("parked_entity"): EntitySelector(EntitySelectorConfig(domain="binary_sensor")),
})


class ConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    VERSION = 1

    async def async_step_user(self, user_input=None):
        errors = {}
        if user_input is not None:
            try:
                client = PiClient(async_get_clientsession(self.hass), user_input["url"],
                                  user_input["token"], user_input["fingerprint"])
                data = await client.status()
            except ValueError:
                errors["base"] = "invalid_settings"
            except PiAuthError:
                errors["base"] = "invalid_auth"
            except PiError:
                errors["base"] = "cannot_connect"
            else:
                await self.async_set_unique_id(data["id"])
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="XpengUSB", data={**user_input, "url": client.url})
        return self.async_show_form(step_id="user", data_schema=SCHEMA, errors=errors)
