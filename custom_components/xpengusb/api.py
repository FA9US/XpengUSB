"""HTTPS client for the small, versioned Pi endpoint."""
import asyncio

from aiohttp import ClientError, ClientTimeout, Fingerprint
from yarl import URL


class PiError(Exception):
    """User-visible Pi error."""


class PiAuthError(PiError):
    """The Pi rejected the HA token."""


class PiClient:
    def __init__(self, session, url, token, fingerprint):
        endpoint = URL(url.strip())
        if (endpoint.scheme != "https" or not endpoint.host or endpoint.user is not None
                or endpoint.path != "/" or endpoint.query or endpoint.fragment):
            raise ValueError("Enter an HTTPS dashboard URL without a path or login.")
        if not token.strip():
            raise ValueError("Enter the Pi's Home Assistant token.")
        self.url = str(endpoint).rstrip("/")
        self.session = session
        self.headers = {"Authorization": "Bearer " + token.strip()}
        self.fingerprint = Fingerprint(bytes.fromhex(fingerprint.replace(":", "").strip()))

    async def request(self, action="ha-status"):
        try:
            async with self.session.request(
                "GET" if action == "ha-status" else "POST",
                self.url + "/api/v1/" + action, ssl=self.fingerprint, headers=self.headers,
                timeout=ClientTimeout(total=55 if action == "ha-test-nas" else 15),
                allow_redirects=False,
            ) as response:
                if response.status == 401:
                    raise PiAuthError("The Pi rejected the Home Assistant token.")
                result = await response.json()
                if not isinstance(result, dict):
                    raise PiError("The Pi returned an unexpected response.")
                if response.status != 200:
                    raise PiError(str(result.get("error", "The Pi could not complete the action."))[:240])
                return result
        except (ClientError, asyncio.TimeoutError, ValueError) as error:
            raise PiError("Could not reach the Pi or verify its HTTPS certificate.") from error

    async def status(self):
        data = await self.request()
        if data.get("version") != 1 or not isinstance(data.get("id"), str):
            raise PiError("This Pi does not have a compatible XpengUSB API.")
        return data
