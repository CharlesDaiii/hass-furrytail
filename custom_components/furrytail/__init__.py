"""FurryTail Home Assistant integration."""

from __future__ import annotations

import asyncio
import logging

import aiohttp

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.aiohttp_client import async_get_clientsession

from .api import FurryTailApiError, FurryTailAuthenticationError, FurryTailClient
from .const import (
    CONF_ACCOUNT_ID,
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
from .coordinator import FurryTailCoordinator

_LOGGER = logging.getLogger(__name__)

PLATFORMS: list[Platform] = [
    Platform.SENSOR,
    Platform.BINARY_SENSOR,
    Platform.SWITCH,
    Platform.BUTTON,
]


async def _async_update_listener(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload an entry after its polling option changes."""
    await hass.config_entries.async_reload(entry.entry_id)


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up FurryTail from a config entry."""
    legacy_product_key = entry.data.get(CONF_ACCOUNT_ID)
    product_key = (
        entry.data.get(CONF_PRODUCT_KEY)
        or legacy_product_key
        or DEFAULT_PRODUCT_KEY
    )
    token = entry.data.get(CONF_TOKEN)
    if not token:
        raise ConfigEntryAuthFailed(
            "A FurryTail app access token is required; reauthentication is needed"
        )

    client = FurryTailClient(
        async_get_clientsession(hass),
        token=token,
        account_id=legacy_product_key,
        product_key=product_key,
        device_id=entry.data.get(CONF_DEVICE_ID),
        thing_name=entry.data.get(CONF_THING_NAME),
        mac=entry.data.get(CONF_MAC),
        place_id=entry.data.get(CONF_PLACE_ID) or None,
    )
    try:
        await client.async_validate_token(
            token, entry.data.get(CONF_MAC) or DEFAULT_MAC
        )
    except FurryTailAuthenticationError as err:
        raise ConfigEntryAuthFailed from err

    try:
        await client.async_list_devices()
        client.select_device(entry.data.get(CONF_MAC))
    except FurryTailAuthenticationError as err:
        raise ConfigEntryAuthFailed from err
    except (
        aiohttp.ClientError,
        asyncio.TimeoutError,
        FurryTailApiError,
    ) as err:
        _LOGGER.warning(
            "FurryTail token validation succeeded but device discovery failed; "
            "continuing with stored details: %s",
            err,
        )

    discovered = {
        CONF_MAC: client.mac or (
            normalize_mac(entry.data.get(CONF_MAC))
            if entry.data.get(CONF_MAC)
            else ""
        ),
        CONF_PRODUCT_KEY: client.product_key or product_key or DEFAULT_PRODUCT_KEY,
        CONF_DEVICE_ID: client.device_id or entry.data.get(CONF_DEVICE_ID, ""),
        CONF_PLACE_ID: client.place_id or entry.data.get(CONF_PLACE_ID, ""),
        CONF_THING_NAME: (
            client.thing_name
            or entry.data.get(CONF_THING_NAME)
            or client.mac
            or ""
        ),
    }
    updated_data = dict(entry.data)
    changed = False
    for key, value in discovered.items():
        if updated_data.get(key) != value:
            updated_data[key] = value
            changed = True
    if changed:
        hass.config_entries.async_update_entry(entry, data=updated_data)

    coordinator = FurryTailCoordinator(
        hass,
        client,
        discovered[CONF_MAC],
        product_key=discovered[CONF_PRODUCT_KEY],
        thing_name=discovered[CONF_THING_NAME],
        device_id=discovered[CONF_DEVICE_ID],
        account_id=legacy_product_key,
        scan_interval=int(
            entry.options.get(CONF_SCAN_INTERVAL, DEFAULT_SCAN_INTERVAL)
        ),
    )
    await coordinator.async_config_entry_first_refresh()

    hass.data.setdefault(DOMAIN, {})[entry.entry_id] = coordinator
    entry.async_on_unload(entry.add_update_listener(_async_update_listener))
    entry.async_create_background_task(
        hass,
        coordinator.async_setup_mqtt(),
        "furrytail_mqtt_setup",
    )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a FurryTail config entry."""
    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        coordinator: FurryTailCoordinator = hass.data[DOMAIN].pop(entry.entry_id)
        await coordinator.async_disconnect_mqtt()
        if not hass.data[DOMAIN]:
            hass.data.pop(DOMAIN)
    return unloaded
