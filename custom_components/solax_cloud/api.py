"""Client for the Solax Cloud realtime API (SolaXCloud User API V2)."""

from __future__ import annotations

from typing import Any

import aiohttp

from homeassistant.util.json import json_loads

REALTIME_PATH = "/api/v2/dataAccess/realtimeInfo/get"
REQUEST_TIMEOUT = aiohttp.ClientTimeout(total=20)

# success=false codes that mean the token or serial is refused: 103 "token
# invalid!" (seen live, undocumented), 1001 "Interface Unauthorized" and
# 1003 "Data Unauthorized" / "no auth!" for a serial outside the account.
AUTH_ERROR_CODES = frozenset({103, 1001, 1003})


class SolaxCloudError(Exception):
    """Base class for Solax Cloud errors."""


class SolaxCloudConnectionError(SolaxCloudError):
    """The API could not be reached or did not answer with JSON."""


class SolaxCloudAuthError(SolaxCloudError):
    """The API refused the token or the serial number."""


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
        if body.get("success") is True and isinstance(body.get("result"), dict):
            return body["result"]

        code, message = body.get("code"), body.get("exception")
        if code in AUTH_ERROR_CODES:
            raise SolaxCloudAuthError(f"{message} (code {code})")
        raise SolaxCloudApiError(f"{message} (code {code})")
