"""Client for the Solax Cloud realtime API (SolaXCloud User API V2)."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.util.json import json_loads

REALTIME_PATH = "/api/v2/dataAccess/realtimeInfo/get"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# success=false codes for a refused token: 103 "token invalid!" (seen live,
# undocumented) and 1001 "Interface Unauthorized".
TOKEN_ERROR_CODES = frozenset({103, 1001})
# 1003 "Data Unauthorized" / "no auth!": the serial is not in the token's
# account (e.g. the inverter serial instead of the dongle's, or a new dongle).
SERIAL_ERROR_CODES = frozenset({1003})


class SolaxCloudError(Exception):
    """Base class for Solax Cloud errors."""


class SolaxCloudConnectionError(SolaxCloudError):
    """The API could not be reached or did not answer with JSON."""


class SolaxCloudAuthError(SolaxCloudError):
    """The API refused the token or the serial number."""


class SolaxCloudTokenError(SolaxCloudAuthError):
    """The API refused the token."""


class SolaxCloudSerialError(SolaxCloudAuthError):
    """The serial number is not in the token's account."""


class SolaxCloudApiError(SolaxCloudError):
    """The API refused the request for another reason."""


def normalize_api_address(address: str) -> str:
    """Turn an API address as pasted from Solax Cloud into a base URL."""
    address = address.strip().rstrip("/")
    if "://" not in address:
        address = f"https://{address}"
    return address


class SolaxCloudClient:
    """Fetches realtime data for one dongle."""

    def __init__(
        self,
        session: aiohttp.ClientSession,
        api_address: str,
        token: str,
        serial: str,
    ) -> None:
        """Initialize the client."""
        self._session = session
        self._url = normalize_api_address(api_address) + REALTIME_PATH
        self._token = token.strip()
        self._serial = serial.strip()

    @property
    def serial(self) -> str:
        """Serial number of the dongle."""
        return self._serial

    async def async_get_realtime_data(self) -> dict[str, Any]:
        """Return the realtime result object."""
        try:
            async with self._session.post(
                self._url,
                json={"wifiSn": self._serial},
                headers={"tokenId": self._token},
                timeout=REQUEST_TIMEOUT,
            ) as response:
                response.raise_for_status()
                # HA's parser, as in tests; it rejects NaN/Infinity tokens.
                body = await response.json(content_type=None, loads=json_loads)
        except (aiohttp.ClientError, TimeoutError, ValueError) as err:
            raise SolaxCloudConnectionError(
                f"Error talking to Solax Cloud: {err!r}"
            ) from err

        if not isinstance(body, dict):
            raise SolaxCloudConnectionError(f"Unexpected response: {body!r:.200}")
        if body.get("success") is True:
            if isinstance(result := body.get("result"), dict):
                return result
            raise SolaxCloudApiError("Solax Cloud has no data for this dongle yet")

        code, message = body.get("code"), body.get("exception")
        if code in TOKEN_ERROR_CODES:
            raise SolaxCloudTokenError(f"{message} (code {code})")
        if code in SERIAL_ERROR_CODES:
            raise SolaxCloudSerialError(f"{message} (code {code})")
        raise SolaxCloudApiError(f"{message} (code {code})")
