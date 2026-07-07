"""Config flow for Mediola Shutters integration."""
import logging
from typing import Any, Dict, Optional

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import FlowResult
import homeassistant.helpers.config_validation as cv

from .const import CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL, DOMAIN
from .mediola_api import MediolaAPI

_LOGGER = logging.getLogger(__name__)


def _build_config_schema(defaults: Optional[Dict[str, Any]] = None) -> vol.Schema:
    """Build the schema for initial setup and reconfiguration."""
    defaults = defaults or {}

    return vol.Schema(
        {
            vol.Required(CONF_HOST, default=defaults.get(CONF_HOST, "")): cv.string,
            vol.Required(CONF_USERNAME, default=defaults.get(CONF_USERNAME, "")): cv.string,
            vol.Required(CONF_PASSWORD, default=defaults.get(CONF_PASSWORD, "")): cv.string,
            vol.Optional(
                CONF_SCAN_INTERVAL,
                default=defaults.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ): vol.All(cv.positive_int, vol.Range(min=5, max=300)),
        }
    )


STEP_USER_DATA_SCHEMA = _build_config_schema()


async def validate_input(hass: HomeAssistant, data: Dict[str, Any]) -> Dict[str, Any]:
    """Validate the user input allows us to connect.
    
    Data has the keys from STEP_USER_DATA_SCHEMA with values provided by the user.
    """
    api = MediolaAPI(data[CONF_HOST], data[CONF_USERNAME], data[CONF_PASSWORD])

    # Try to connect and get states
    try:
        shutters = await hass.async_add_executor_job(api.get_states)
    except Exception as err:
        _LOGGER.error("Could not connect to Mediola gateway: %s", err)
        raise CannotConnect

    # Return info that we want to store in the config entry
    return {
        "title": f"Mediola Gateway ({data[CONF_HOST]})",
        "num_shutters": len(shutters),
    }


class MediolaConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Mediola Shutters."""

    VERSION = 1

    async def async_step_user(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle the initial step."""
        errors: Dict[str, str] = {}

        if user_input is not None:
            try:
                info = await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception")
                errors["base"] = "unknown"
            else:
                # Create a unique ID based on the host
                await self.async_set_unique_id(user_input[CONF_HOST])
                self._abort_if_unique_id_configured()

                return self.async_create_entry(title=info["title"], data=user_input)

        return self.async_show_form(
            step_id="user", data_schema=STEP_USER_DATA_SCHEMA, errors=errors
        )

    async def async_step_reconfigure(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Handle reconfiguration of an existing entry."""
        errors: Dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            try:
                await validate_input(self.hass, user_input)
            except CannotConnect:
                errors["base"] = "cannot_connect"
            except Exception:  # pylint: disable=broad-except
                _LOGGER.exception("Unexpected exception during reconfiguration")
                errors["base"] = "unknown"
            else:
                for existing_entry in self._async_current_entries():
                    if (
                        existing_entry.entry_id != entry.entry_id
                        and existing_entry.unique_id == user_input[CONF_HOST]
                    ):
                        errors["base"] = "already_configured"
                        break

                if not errors:
                    updated_options = dict(entry.options)
                    updated_options[CONF_SCAN_INTERVAL] = user_input[CONF_SCAN_INTERVAL]

                    self.hass.config_entries.async_update_entry(
                        entry,
                        title=f"Mediola Gateway ({user_input[CONF_HOST]})",
                        data=user_input,
                        options=updated_options,
                        unique_id=user_input[CONF_HOST],
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reconfigure_successful")

        current_data = {
            CONF_HOST: entry.data.get(CONF_HOST, ""),
            CONF_USERNAME: entry.data.get(CONF_USERNAME, ""),
            CONF_PASSWORD: entry.data.get(CONF_PASSWORD, ""),
            CONF_SCAN_INTERVAL: entry.options.get(
                CONF_SCAN_INTERVAL,
                entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
            ),
        }

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_build_config_schema(current_data),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> "MediolaOptionsFlowHandler":
        """Get the options flow for this handler."""
        return MediolaOptionsFlowHandler(config_entry)


class MediolaOptionsFlowHandler(config_entries.OptionsFlow):
    """Handle options flow for Mediola Shutters integration."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize options flow."""
        self.config_entry = config_entry

    async def async_step_init(
        self, user_input: Optional[Dict[str, Any]] = None
    ) -> FlowResult:
        """Manage the options."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        # Get current scan interval
        current_scan_interval = self.config_entry.options.get(
            CONF_SCAN_INTERVAL,
            self.config_entry.data.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL),
        )

        options_schema = vol.Schema(
            {
                vol.Optional(
                    CONF_SCAN_INTERVAL, default=current_scan_interval
                ): vol.All(cv.positive_int, vol.Range(min=5, max=300)),
            }
        )

        return self.async_show_form(step_id="init", data_schema=options_schema)


class CannotConnect(Exception):
    """Error to indicate we cannot connect."""