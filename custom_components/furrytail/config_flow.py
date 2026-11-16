"""Config flow for the FurryTail integration."""

from __future__ import annotations

import asyncio
import hashlib
import re
from typing import Any

import aiohttp
import voluptuous as vol

from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FurryTailApiError, FurryTailAuthenticationError, FurryTailClient
from .const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    CONF_PLACE_ID,
    CONF_PRODUCT_KEY,
    CONF_SCAN_INTERVAL,
    CONF_THING_NAME,
    CONF_TOKEN,
    DEFAULT_MAC,
    DEFAULT_PRODUCT_KEY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    normalize_mac,
)

_MAC_PATTERN = re.compile(
    r"^(?:(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}|[0-9A-Fa-f]{12})$"
)


def _text_selector(text_type: selector.TextSelectorType) -> selector.TextSelector:
    """Build one text selector with a semantic input type."""
    return selector.TextSelector(selector.TextSelectorConfig(type=text_type))


def _token_schema(defaults: dict[str, Any] | None = None) -> vol.Schema:
    """Return the token and optional MAC form schema."""
    defaults = defaults or {}
    token_marker: dict[str, Any] = {}
    if defaults.get(CONF_TOKEN):
        token_marker["default"] = defaults[CONF_TOKEN]
    mac_marker: dict[str, Any] = {}
    if defaults.get(CONF_MAC):
        mac_marker["default"] = defaults[CONF_MAC]
    place_marker: dict[str, Any] = {}
    if defaults.get(CONF_PLACE_ID):
        place_marker["default"] = defaults[CONF_PLACE_ID]
    return vol.Schema(
        {
            vol.Required(CONF_TOKEN, **token_marker): _text_selector(
                selector.TextSelectorType.PASSWORD
            ),
            vol.Optional(CONF_MAC, **mac_marker): _text_selector(
                selector.TextSelectorType.TEXT
            ),
            vol.Optional(CONF_PLACE_ID, **place_marker): _text_selector(
                selector.TextSelectorType.TEXT
            ),
        }
    )


def _valid_mac(mac: str | None) -> bool:
    """Return whether an optional MAC has the documented separated format."""
    return not mac or _MAC_PATTERN.fullmatch(mac.strip()) is not None


def _discovery_fallback(
    client: FurryTailClient, requested_mac: str | None
) -> dict[str, str]:
    """Build stored device details when best-effort discovery fails."""
    fallback_mac = (
        normalize_mac(requested_mac) if requested_mac else client.mac or ""
    )
    return {
        CONF_MAC: fallback_mac,
        CONF_PRODUCT_KEY: DEFAULT_PRODUCT_KEY,
        CONF_DEVICE_ID: "",
        CONF_THING_NAME: fallback_mac,
    }


async def _async_discover_device(
    client: FurryTailClient, requested_mac: str | None
) -> dict[str, str]:
    """Discover and select the requested device, or the account's first device."""
    await client.async_list_devices()
    selected = client.select_device(requested_mac)

    fallback = _discovery_fallback(client, requested_mac)
    if selected is None:
        return fallback
    mac = str(selected.get("mac") or fallback[CONF_MAC])
    return {
        CONF_MAC: mac,
        CONF_PRODUCT_KEY: str(client.product_key or DEFAULT_PRODUCT_KEY),
        CONF_DEVICE_ID: str(client.device_id or ""),
        CONF_THING_NAME: str(client.thing_name or mac),
    }


def _entry_unique_id(token: str, mac: str | None) -> str:
    """Build a stable device ID, with a token-derived fallback."""
    if mac:
        return normalize_mac(mac)
    return hashlib.sha256(token.encode()).hexdigest()[:16]


class FurryTailConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle FurryTail configuration and reauthentication."""

    VERSION = 1

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Handle the initial setup form."""
        errors: dict[str, str] = {}
        if user_input is not None:
            if not _valid_mac(user_input.get(CONF_MAC)):
                errors[CONF_MAC] = "invalid_mac"
            else:
                token = user_input[CONF_TOKEN].strip()
                requested_mac = (user_input.get(CONF_MAC) or "").strip()
                client = FurryTailClient(
                    async_get_clientsession(self.hass),
                    token=token,
                    mac=requested_mac or None,
                    place_id=(user_input.get(CONF_PLACE_ID) or "").strip()
                    or None,
                )
                try:
                    await client.async_validate_token(
                        token, requested_mac or DEFAULT_MAC
                    )
                    try:
                        discovery = await _async_discover_device(
                            client, requested_mac or None
                        )
                    except Exception:
                        discovery = _discovery_fallback(
                            client, requested_mac or None
                        )
                except FurryTailAuthenticationError:
                    errors["base"] = "invalid_auth"
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    FurryTailApiError,
                ):
                    errors["base"] = "cannot_connect"
                else:
                    await self.async_set_unique_id(
                        _entry_unique_id(token, discovery[CONF_MAC])
                    )
                    self._abort_if_unique_id_configured()
                    data = {
                        CONF_TOKEN: token,
                        CONF_PLACE_ID: client.place_id
                        or (user_input.get(CONF_PLACE_ID) or "").strip(),
                        **discovery,
                    }
                    title = (
                        f"FurryTail {data[CONF_MAC]}"
                        if data[CONF_MAC]
                        else "FurryTail"
                    )
                    return self.async_create_entry(title=title, data=data)

        return self.async_show_form(
            step_id="user",
            data_schema=_token_schema(user_input),
            errors=errors,
        )

    async def async_step_reauth(
        self, entry_data: dict[str, Any]
    ) -> FlowResult:
        """Begin reauthentication for an existing entry."""
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Validate and save replacement credentials."""
        entry = self.hass.config_entries.async_get_entry(self.context["entry_id"])
        if entry is None:
            return self.async_abort(reason="reauth_successful")

        errors: dict[str, str] = {}
        if user_input is not None:
            if not _valid_mac(user_input.get(CONF_MAC)):
                errors[CONF_MAC] = "invalid_mac"
            else:
                token = user_input[CONF_TOKEN].strip()
                requested_mac = (user_input.get(CONF_MAC) or "").strip()
                validation_mac = (
                    requested_mac or entry.data.get(CONF_MAC) or DEFAULT_MAC
                )
                client = FurryTailClient(
                    async_get_clientsession(self.hass),
                    token=token,
                    mac=requested_mac or entry.data.get(CONF_MAC),
                    place_id=(user_input.get(CONF_PLACE_ID) or "").strip()
                    or entry.data.get(CONF_PLACE_ID)
                    or None,
                )
                try:
                    await client.async_validate_token(token, validation_mac)
                    discovery_mac = requested_mac or entry.data.get(CONF_MAC)
                    try:
                        discovery = await _async_discover_device(
                            client, discovery_mac
                        )
                    except Exception:
                        discovery = _discovery_fallback(client, discovery_mac)
                except FurryTailAuthenticationError:
                    errors["base"] = "invalid_auth"
                except (
                    aiohttp.ClientError,
                    asyncio.TimeoutError,
                    FurryTailApiError,
                ):
                    errors["base"] = "cannot_connect"
                else:
                    for key in (
                        CONF_MAC,
                        CONF_PRODUCT_KEY,
                        CONF_DEVICE_ID,
                        CONF_THING_NAME,
                    ):
                        discovery[key] = (
                            discovery.get(key)
                            or entry.data.get(key)
                            or (DEFAULT_PRODUCT_KEY if key == CONF_PRODUCT_KEY else "")
                        )
                    updated_data = dict(entry.data)
                    updated_data.pop("email", None)
                    updated_data.pop("password", None)
                    updated_data.update({CONF_TOKEN: token, **discovery})
                    updated_data[CONF_PLACE_ID] = (
                        client.place_id
                        or (user_input.get(CONF_PLACE_ID) or "").strip()
                        or entry.data.get(CONF_PLACE_ID, "")
                    )
                    self.hass.config_entries.async_update_entry(
                        entry,
                        data=updated_data,
                        unique_id=_entry_unique_id(token, discovery[CONF_MAC]),
                    )
                    await self.hass.config_entries.async_reload(entry.entry_id)
                    return self.async_abort(reason="reauth_successful")

        defaults = {CONF_MAC: entry.data.get(CONF_MAC, "")}
        if user_input:
            defaults.update(user_input)
        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=_token_schema(defaults),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> FurryTailOptionsFlow:
        """Return the options flow handler."""
        return FurryTailOptionsFlow(config_entry)


class FurryTailOptionsFlow(config_entries.OptionsFlow):
    """Handle the polling interval option."""

    def __init__(self, config_entry: config_entries.ConfigEntry) -> None:
        """Initialize the options handler."""
        self._config_entry = config_entry

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Show and save the polling interval and the place ID."""
        if user_input is not None:
            place_id = str(user_input.get(CONF_PLACE_ID) or "").strip()
            if place_id and place_id != self._config_entry.data.get(CONF_PLACE_ID):
                # The place ID belongs to the entry data (the API client
                # reads it from there); the update listener reloads the
                # entry, so the new value takes effect immediately.
                self.hass.config_entries.async_update_entry(
                    self._config_entry,
                    data={**self._config_entry.data, CONF_PLACE_ID: place_id},
                )
            return self.async_create_entry(
                title="",
                data={CONF_SCAN_INTERVAL: int(user_input[CONF_SCAN_INTERVAL])},
            )

        current = self._config_entry.options.get(
            CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL
        )
        current_place = self._config_entry.data.get(CONF_PLACE_ID, "")
        schema = vol.Schema(
            {
                vol.Required(CONF_SCAN_INTERVAL, default=current): selector.NumberSelector(
                    selector.NumberSelectorConfig(
                        min=10,
                        max=300,
                        step=1,
                        mode=selector.NumberSelectorMode.BOX,
                        unit_of_measurement="seconds",
                    )
                ),
                vol.Optional(CONF_PLACE_ID, default=current_place): selector.TextSelector(
                    selector.TextSelectorConfig(
                        type=selector.TextSelectorType.TEXT
                    )
                ),
            }
        )
        return self.async_show_form(step_id="init", data_schema=schema)
