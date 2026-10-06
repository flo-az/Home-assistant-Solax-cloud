"""Config flow for Solax Cloud integration."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.selector import (
    TextSelector,
    TextSelectorConfig,
    TextSelectorType,
)

from .api import (
    SolaxCloudApiError,
    SolaxCloudClient,
    SolaxCloudConnectionError,
    SolaxCloudSerialError,
    SolaxCloudTokenError,
    normalize_api_address,
)
from .const import (
    CONF_API_ADDRESS,
    CONF_SERIAL,
    CONF_TOKEN,
    DEFAULT_API_ADDRESS,
    DOMAIN,
)

API_ADDRESS_FIELD = {
    vol.Required(CONF_API_ADDRESS, default=DEFAULT_API_ADDRESS): TextSelector(
        TextSelectorConfig(type=TextSelectorType.URL)
    )
}
TOKEN_FIELD = {
    vol.Required(CONF_TOKEN): TextSelector(
        TextSelectorConfig(type=TextSelectorType.PASSWORD)
    )
}


class SolaxCloudConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Solax Cloud."""

    VERSION = 1

    async def _async_validate(
        self, api_address: str, token: str, serial: str
    ) -> dict[str, str]:
        """Try the credentials against the API; return form errors."""
        client = SolaxCloudClient(
            async_get_clientsession(self.hass), api_address, token, serial
        )
        try:
            await client.async_get_realtime_data()
        except SolaxCloudTokenError:
            return {"base": "invalid_token"}
        except SolaxCloudSerialError:
            return {"base": "serial_not_in_account"}
        except SolaxCloudConnectionError:
            return {"base": "cannot_connect"}
        except SolaxCloudApiError:
            return {"base": "api_error"}
        return {}

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Step when user initializes a integration."""
        errors: dict[str, str] = {}

        if user_input is not None:
            api_address = normalize_api_address(user_input[CONF_API_ADDRESS])
            token = user_input[CONF_TOKEN]
            serial = user_input[CONF_SERIAL].strip()

            await self.async_set_unique_id(f"SolaxCloud_{serial}")
            self._abort_if_unique_id_configured()

            errors = await self._async_validate(api_address, token, serial)
            if not errors:
                return self.async_create_entry(
                    title=serial,
                    data={
                        CONF_API_ADDRESS: api_address,
                        CONF_TOKEN: token.strip(),
                        CONF_SERIAL: serial,
                    },
                )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema(
                    {
                        **API_ADDRESS_FIELD,
                        **TOKEN_FIELD,
                        vol.Required(CONF_SERIAL): TextSelector(),
                    }
                ),
                user_input,
            ),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Solax Cloud refused the stored token."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Ask for a token that works with the current API."""
        entry = self._get_reauth_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            api_address = normalize_api_address(user_input[CONF_API_ADDRESS])
            errors = await self._async_validate(
                api_address, user_input[CONF_TOKEN], entry.data[CONF_SERIAL]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_ADDRESS: api_address,
                        CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                    },
                )

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({**API_ADDRESS_FIELD, **TOKEN_FIELD}),
                {
                    CONF_API_ADDRESS: entry.data.get(
                        CONF_API_ADDRESS, DEFAULT_API_ADDRESS
                    )
                },
            ),
            description_placeholders={"serial": entry.data[CONF_SERIAL]},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Change the API address or token of an existing entry."""
        entry = self._get_reconfigure_entry()
        errors: dict[str, str] = {}

        if user_input is not None:
            api_address = normalize_api_address(user_input[CONF_API_ADDRESS])
            errors = await self._async_validate(
                api_address, user_input[CONF_TOKEN], entry.data[CONF_SERIAL]
            )
            if not errors:
                return self.async_update_reload_and_abort(
                    entry,
                    data_updates={
                        CONF_API_ADDRESS: api_address,
                        CONF_TOKEN: user_input[CONF_TOKEN].strip(),
                    },
                )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=self.add_suggested_values_to_schema(
                vol.Schema({**API_ADDRESS_FIELD, **TOKEN_FIELD}),
                {
                    CONF_API_ADDRESS: entry.data.get(
                        CONF_API_ADDRESS, DEFAULT_API_ADDRESS
                    )
                },
            ),
            description_placeholders={"serial": entry.data[CONF_SERIAL]},
            errors=errors,
        )
