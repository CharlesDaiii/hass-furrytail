"""Data coordinator for the FurryTail integration."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
import logging
import time
from typing import Any

from aiohttp import ClientError

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import (
    DataUpdateCoordinator,
    UpdateFailed,
)

from .api import FurryTailApiError, FurryTailAuthenticationError, FurryTailClient
from .const import (
    DEFAULT_PRODUCT_KEY,
    DEFAULT_SCAN_INTERVAL,
    DOMAIN,
    build_control_topic,
    build_update_topic,
    normalize_mac,
)
from .mqtt_client import (
    FurryTailMqttClient,
    FurryTailMqttError,
    build_aws_iot_wss_path,
)

_LOGGER = logging.getLogger(__name__)
_MQTT_CREDENTIAL_REFRESH_LEEWAY = 300
_MQTT_SETUP_RETRY_COOLDOWN = 60


def _expiration_epoch(value: Any) -> float | None:
    """Convert an AWS numeric or ISO-8601 expiration to epoch seconds."""
    if value is None:
        return None
    try:
        numeric = float(value)
        if numeric > 10_000_000_000:
            numeric /= 1000
        return numeric
    except (TypeError, ValueError):
        pass
    try:
        text = str(value).replace("Z", "+00:00")
        parsed = datetime.fromisoformat(text)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.timestamp()
    except ValueError:
        return None


class FurryTailCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate HTTP source-of-truth polling and MQTT push merges."""

    def __init__(
        self,
        hass: HomeAssistant,
        client: FurryTailClient,
        mac: str | None,
        product_key: str | None = None,
        thing_name: str | None = None,
        device_id: str | None = None,
        account_id: str | None = None,
        mqtt_client: FurryTailMqttClient | None = None,
        scan_interval: int = DEFAULT_SCAN_INTERVAL,
    ) -> None:
        """Initialize the coordinator, accepting account_id only for 0.1.0."""
        super().__init__(
            hass,
            _LOGGER,
            name=DOMAIN,
            update_interval=timedelta(seconds=scan_interval),
        )
        self.client = client
        self.mac = normalize_mac(mac) if mac else ""
        self.product_key = product_key or account_id or DEFAULT_PRODUCT_KEY
        self.thing_name = normalize_mac(thing_name) if thing_name else self.mac
        self.device_id = device_id or ""
        self.mqtt_client = mqtt_client
        self.mqtt_connected_at: float | None = None
        self._credentials_expiration: float | None = None
        self._mqtt_unavailable_logged = False
        self._mqtt_setup_task: asyncio.Task[None] | None = None
        self._mqtt_setup_last_attempt: float = 0.0

    @property
    def device_info(self) -> DeviceInfo:
        """Return the registry metadata shared by every entity."""
        identifier = self.mac or self.device_id or "undiscovered"
        return DeviceInfo(
            identifiers={(DOMAIN, identifier)},
            name="Automatic Cat Litter Box",
            manufacturer="FurryTail",
            model="Automatic Cat Litter Box",
        )

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch the latest HTTP snapshot and retain MQTT as a push overlay."""
        if not self.mac and self.client.mac:
            self.mac = normalize_mac(self.client.mac)
            if not self.thing_name:
                self.thing_name = self.mac
        mqtt_client = self.mqtt_client
        if mqtt_client is not None:
            mqtt_connected = mqtt_client.is_connected()
            if mqtt_connected:
                self.mqtt_connected_at = mqtt_client.connected_at

            credentials_near_expiry = (
                self._credentials_expiration is not None
                and self._credentials_expiration
                <= time.time() + _MQTT_CREDENTIAL_REFRESH_LEEWAY
            )
            mqtt_setup_pending = (
                self._mqtt_setup_task is not None
                and not self._mqtt_setup_task.done()
            )
            now = time.monotonic()
            if (
                (
                    not mqtt_connected
                    or mqtt_client.needs_credential_refresh
                    or credentials_near_expiry
                )
                and not mqtt_setup_pending
                and now - self._mqtt_setup_last_attempt
                >= _MQTT_SETUP_RETRY_COOLDOWN
            ):
                self._mqtt_setup_last_attempt = now
                self._mqtt_setup_task = self.hass.async_create_task(
                    self.async_setup_mqtt(), "furrytail_mqtt_refresh"
                )

        # The SDK also forces HTTP for the first 10 seconds after MQTT connects.
        # This coordinator deliberately keeps periodic HTTP polling at all times
        # as the source of truth, while MQTT frames are merged between polls.
        if not self.mac:
            _LOGGER.info(
                "No FurryTail MAC was configured or discovered; attributes "
                "cannot be polled yet"
            )
            return dict(self.data or {})
        try:
            return await self.client.async_get_attributes(self.mac)
        except FurryTailAuthenticationError as err:
            raise ConfigEntryAuthFailed from err
        except (ClientError, FurryTailApiError, TimeoutError, ValueError) as err:
            raise UpdateFailed(f"Unable to fetch FurryTail attributes: {err}") from err

    async def async_setup_mqtt(self) -> None:
        """Start AWS IoT WSS when a complete temporary credential set exists."""
        setup_task = asyncio.current_task()
        if (
            self._mqtt_setup_task is not None
            and self._mqtt_setup_task is not setup_task
            and not self._mqtt_setup_task.done()
        ):
            return
        if setup_task is not None:
            self._mqtt_setup_task = setup_task
        self._mqtt_setup_last_attempt = time.monotonic()
        try:
            if self.mqtt_client is not None:
                credentials_near_expiry = (
                    self._credentials_expiration is not None
                    and self._credentials_expiration
                    <= time.time() + _MQTT_CREDENTIAL_REFRESH_LEEWAY
                )
                if (
                    self.mqtt_client.is_connected()
                    and not self.mqtt_client.needs_credential_refresh
                    and not credentials_near_expiry
                ):
                    self.mqtt_connected_at = self.mqtt_client.connected_at
                    return
                # A refresh setup always obtains fresh temporary keys.
                await self.hass.async_add_executor_job(
                    self.mqtt_client.disconnect
                )
                self.mqtt_client = None
                self.mqtt_connected_at = None

            policy = await self.client.async_get_aws_credentials()
            mode = policy.get("mode")
            endpoint = policy.get("endpoint")
            region = policy.get("region")
            if mode is None or not endpoint or not region:
                if not self._mqtt_unavailable_logged:
                    _LOGGER.info(
                        "FurryTail AWS connection details are incomplete; "
                        "continuing in polling-only mode"
                    )
                    self._mqtt_unavailable_logged = True
                return

            if mode == "sts":
                expiration = _expiration_epoch(policy.get("credentials_expiration"))
                if (
                    expiration is not None
                    and expiration
                    <= time.time() + _MQTT_CREDENTIAL_REFRESH_LEEWAY
                ):
                    policy = await self.client.async_get_aws_credentials()
                    mode = policy.get("mode")
                    endpoint = policy.get("endpoint")
                    region = policy.get("region")
                    expiration = _expiration_epoch(
                        policy.get("credentials_expiration")
                    )
                raw_credentials = policy.get("credentials") or {}
                credentials = {
                    "access_key_id": raw_credentials.get("accessKeyId"),
                    "secret_access_key": raw_credentials.get("secretAccessKey"),
                    "session_token": raw_credentials.get("sessionToken"),
                    "expiration": policy.get("credentials_expiration"),
                }
            elif mode == "cognito":
                credentials = await self.client.async_exchange_cognito_credentials(
                    policy
                )
                expiration = _expiration_epoch(credentials.get("expiration"))
            else:
                return

            if not all(
                credentials.get(key)
                for key in ("access_key_id", "secret_access_key", "session_token")
            ):
                _LOGGER.warning(
                    "FurryTail returned incomplete AWS credentials; continuing "
                    "in polling-only mode"
                )
                return

            self.product_key = self.product_key or self.client.product_key or ""
            self.mac = self.mac or self.client.mac or ""
            self.thing_name = (
                self.thing_name or self.client.thing_name or self.mac
            )
            if not self.product_key or not self.thing_name:
                _LOGGER.info(
                    "FurryTail productKey or thingName is unavailable; continuing "
                    "in polling-only mode"
                )
                return

            ws_path = build_aws_iot_wss_path(
                endpoint,
                region,
                credentials["access_key_id"],
                credentials["secret_access_key"],
                credentials["session_token"],
            )
            subscribe_topic = build_update_topic(
                self.product_key, self.thing_name, self.mac
            )
            control_topic = build_control_topic(
                self.product_key, self.thing_name, self.mac
            )
            client_id = policy.get("client_id") or policy.get("identity_id")
            if not client_id:
                _LOGGER.warning(
                    "FurryTail AWS client ID is unavailable; continuing in "
                    "polling-only mode"
                )
                return

            self.mqtt_client = FurryTailMqttClient(
                host=endpoint,
                port=443,
                client_id=client_id,
                username=None,
                password=None,
                subscribe_topic=subscribe_topic,
                control_topic=control_topic,
                on_message=self._mqtt_message_received,
                transport="websockets",
                ws_path=ws_path,
                keepalive=10,
            )
            self._credentials_expiration = expiration
            await self.hass.async_add_executor_job(self.mqtt_client.connect)
            self._mqtt_unavailable_logged = False
        except Exception:  # paho/aiohttp expose several concrete subclasses
            _LOGGER.exception(
                "Unable to start FurryTail MQTT; continuing in polling-only mode"
            )
        finally:
            if self._mqtt_setup_task is setup_task:
                self._mqtt_setup_task = None

    def _mqtt_message_received(
        self, message: dict[str, dict[str, Any]]
    ) -> None:
        """Transfer a paho-thread update safely onto HA's event loop."""
        changes: dict[str, Any] = {}
        changes.update(message.get("value", {}))
        changes.update(message.get("data", {}))
        if changes:
            self.hass.loop.call_soon_threadsafe(self._merge_mqtt_update, changes)

    def _merge_mqtt_update(self, changes: dict[str, Any]) -> None:
        """Merge a live update into the latest coordinator snapshot."""
        merged = dict(self.data or {})
        merged.update(changes)
        self.async_set_updated_data(merged)

    async def async_set_attribute(self, attr_id: int, value: Any) -> None:
        """Control one attribute and turn token rejection into HA reauth."""
        try:
            await self._async_set_attribute(attr_id, value)
        except FurryTailAuthenticationError as err:
            raise ConfigEntryAuthFailed from err

    async def _async_set_attribute(self, attr_id: int, value: Any) -> None:
        """Resolve the type, then control over MQTT or the HTTPS fallback."""
        try:
            data_type = await self.client.async_resolve_data_type(attr_id)
        except FurryTailAuthenticationError:
            raise
        except Exception:
            _LOGGER.warning(
                "Unable to resolve FurryTail data type for attribute %s; using 81",
                attr_id,
                exc_info=True,
            )
            data_type = 81

        if self.mqtt_client is not None and self.mqtt_client.is_connected():
            try:
                await self.hass.async_add_executor_job(
                    self.mqtt_client.publish_control,
                    attr_id,
                    value,
                    data_type,
                )
            except FurryTailMqttError:
                _LOGGER.warning(
                    "FurryTail MQTT control failed; using HTTPS fallback",
                    exc_info=True,
                )
                await self.client.async_set_attribute_http(
                    attr_id, value, data_type
                )
        else:
            await self.client.async_set_attribute_http(attr_id, value, data_type)
        await self.async_request_refresh()

    async def async_disconnect_mqtt(self) -> None:
        """Stop the optional MQTT worker."""
        if self.mqtt_client is not None:
            await self.hass.async_add_executor_job(self.mqtt_client.disconnect)
            self.mqtt_connected_at = None
