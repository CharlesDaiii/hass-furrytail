"""Threaded paho-mqtt client for FurryTail live updates and control."""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import time
from collections.abc import Callable
from datetime import datetime, timezone
from threading import Event, Lock
from typing import Any
from urllib.parse import quote

import paho.mqtt.client as mqtt

from .const import build_control_payload, parse_attribute_value

_LOGGER = logging.getLogger(__name__)

SUBSCRIBE_TOPIC_TEMPLATE = "/{product_key}/{thing_name}/user/update"
CONTROL_TOPIC_TEMPLATE = "/{product_key}/{thing_name}/user/get"


class FurryTailMqttError(Exception):
    """Raised when a FurryTail MQTT operation cannot be completed."""


def _sign(key: bytes, message: str) -> bytes:
    """Return a SHA-256 HMAC used by the SigV4 key ladder."""
    return hmac.new(key, message.encode("utf-8"), hashlib.sha256).digest()


def _aws_quote(value: Any) -> str:
    """Apply AWS's RFC 3986 query-string encoding."""
    return quote(str(value), safe="-_.~")


def build_aws_iot_wss_path(
    endpoint: str,
    region: str,
    access_key: str,
    secret_key: str,
    session_token: str | None,
    *,
    now: datetime | float | int | None = None,
    expires: int = 86400,
) -> str:
    """Build a deterministic SigV4-presigned AWS IoT WebSocket path.

    SigV4 hashes the canonical GET /mqtt request, then derives a signing key
    through date, region, ``iotdata``, and ``aws4_request`` HMAC stages.
    """
    host = endpoint.strip()
    if "://" in host:
        host = host.split("://", 1)[1]
    host = host.split("/", 1)[0]

    if now is None:
        instant = datetime.now(timezone.utc)
    elif isinstance(now, datetime):
        instant = now
        if instant.tzinfo is None:
            instant = instant.replace(tzinfo=timezone.utc)
        else:
            instant = instant.astimezone(timezone.utc)
    else:
        instant = datetime.fromtimestamp(float(now), tz=timezone.utc)

    date_stamp = instant.strftime("%Y%m%d")
    amz_date = instant.strftime("%Y%m%dT%H%M%SZ")
    scope = f"{date_stamp}/{region}/iotdata/aws4_request"
    query: dict[str, Any] = {
        "X-Amz-Algorithm": "AWS4-HMAC-SHA256",
        "X-Amz-Credential": f"{access_key}/{scope}",
        "X-Amz-Date": amz_date,
        "X-Amz-Expires": str(expires),
        "X-Amz-SignedHeaders": "host",
    }
    if session_token:
        query["X-Amz-Security-Token"] = session_token

    canonical_query = "&".join(
        f"{_aws_quote(key)}={_aws_quote(value)}"
        for key, value in sorted(query.items())
    )
    canonical_headers = f"host:{host}\n"
    payload_hash = hashlib.sha256(b"").hexdigest()
    canonical_request = "\n".join(
        (
            "GET",
            "/mqtt",
            canonical_query,
            canonical_headers,
            "host",
            payload_hash,
        )
    )
    string_to_sign = "\n".join(
        (
            "AWS4-HMAC-SHA256",
            amz_date,
            scope,
            hashlib.sha256(canonical_request.encode("utf-8")).hexdigest(),
        )
    )
    date_key = _sign(("AWS4" + secret_key).encode("utf-8"), date_stamp)
    region_key = _sign(date_key, region)
    service_key = _sign(region_key, "iotdata")
    signing_key = _sign(service_key, "aws4_request")
    signature = hmac.new(
        signing_key, string_to_sign.encode("utf-8"), hashlib.sha256
    ).hexdigest()
    return f"/mqtt?{canonical_query}&X-Amz-Signature={signature}"


def parse_mqtt_message(payload: str | bytes) -> dict[str, dict[str, Any]]:
    """Parse and normalize a FurryTail value or data update frame."""
    if isinstance(payload, bytes):
        payload = payload.decode("utf-8")
    decoded = json.loads(payload)
    if not isinstance(decoded, dict):
        raise ValueError("MQTT payload must be a JSON object")

    normalized: dict[str, dict[str, Any]] = {}
    for frame_name in ("value", "data"):
        frame = decoded.get(frame_name)
        if frame is None:
            continue
        if not isinstance(frame, dict):
            raise ValueError(f"MQTT {frame_name} frame must be an object")

        attributes: dict[str, Any] = {}
        for attr_id, attr_data in frame.items():
            raw_value = attr_data
            if isinstance(attr_data, dict) and "value" in attr_data:
                raw_value = attr_data["value"]
            attributes[str(attr_id)] = parse_attribute_value(raw_value)
        normalized[frame_name] = attributes

    if not normalized:
        raise ValueError("MQTT payload contains neither a value nor data frame")
    return normalized


class FurryTailMqttClient:
    """Own a paho client whose network loop runs outside HA's event loop."""

    def __init__(
        self,
        host: str | None,
        port: int | None,
        client_id: str | None,
        username: str | None,
        password: str | None,
        subscribe_topic: str,
        on_message: Callable[[dict[str, dict[str, Any]]], None],
        *,
        control_topic: str | None = None,
        transport: str = "tcp",
        ws_path: str | None = None,
        keepalive: int = 10,
    ) -> None:
        """Initialize the threaded MQTT client without connecting it."""
        self._host = host
        self._port = port or (443 if transport == "websockets" else 8883)
        self._subscribe_topic = subscribe_topic
        self._control_topic = control_topic
        self._on_update = on_message
        self._keepalive = keepalive
        self._connected = Event()
        self._connected_at: float | None = None
        self._needs_credential_refresh = False
        self._loop_started = False
        self._lifecycle_lock = Lock()

        self._client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=client_id or "",
            protocol=mqtt.MQTTv311,
            transport=transport,
        )
        if username is not None:
            self._client.username_pw_set(username, password)
        if transport == "websockets":
            self._client.ws_set_options(path=ws_path or "/mqtt")
            self._client.tls_set()

        self._client.on_connect = self._handle_connect
        self._client.on_disconnect = self._handle_disconnect
        self._client.on_message = self._handle_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    @property
    def connected_at(self) -> float | None:
        """Return the monotonic timestamp of the current connection."""
        return self._connected_at

    @property
    def needs_credential_refresh(self) -> bool:
        """Return whether a disconnect requires a fresh presigned session."""
        return self._needs_credential_refresh

    def connect(self) -> None:
        """Start paho's background loop and asynchronous connection."""
        if not self._host:
            raise FurryTailMqttError("AWS IoT host is unavailable")

        with self._lifecycle_lock:
            if self._loop_started:
                return
            _LOGGER.info("Starting FurryTail MQTT connection")
            self._client.connect_async(
                self._host, self._port, keepalive=self._keepalive
            )
            self._client.loop_start()
            self._loop_started = True

    def disconnect(self) -> None:
        """Disconnect and stop paho's background thread."""
        with self._lifecycle_lock:
            if not self._loop_started:
                return
            self._client.disconnect()
            self._client.loop_stop()
            self._loop_started = False
            self._connected.clear()
            self._connected_at = None
            _LOGGER.info("Stopped FurryTail MQTT connection")

    def is_connected(self) -> bool:
        """Return whether paho has an active broker connection."""
        return self._connected.is_set()

    def publish_control(
        self,
        attr_id: int,
        value: Any,
        data_type: int = 81,
        topic: str | None = None,
    ) -> None:
        """Publish a QoS-1, non-retained device control payload."""
        publish_topic = topic or self._control_topic
        if not publish_topic:
            raise FurryTailMqttError("FurryTail MQTT control topic is unavailable")
        if not self.is_connected():
            raise FurryTailMqttError("FurryTail MQTT client is not connected")

        result = self._client.publish(
            publish_topic,
            json.dumps(
                build_control_payload(attr_id, value, data_type),
                separators=(",", ":"),
            ),
            qos=1,
            retain=False,
        )
        if result.rc != mqtt.MQTT_ERR_SUCCESS:
            raise FurryTailMqttError(
                f"FurryTail MQTT publish failed with code {result.rc}"
            )

    def _handle_connect(
        self,
        client: mqtt.Client,
        userdata: Any,
        flags: mqtt.ConnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        """Subscribe after every successful initial or reconnect."""
        if reason_code != 0:
            _LOGGER.warning("FurryTail MQTT connection failed: %s", reason_code)
            return
        self._connected.set()
        self._connected_at = time.monotonic()
        result, _mid = client.subscribe(self._subscribe_topic, qos=0)
        if result != mqtt.MQTT_ERR_SUCCESS:
            self._connected.clear()
            self._connected_at = None
            _LOGGER.error("FurryTail MQTT subscribe failed with code %s", result)
            return
        _LOGGER.info("FurryTail MQTT connected and subscribed")

    def _handle_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: mqtt.DisconnectFlags,
        reason_code: mqtt.ReasonCode,
        properties: mqtt.Properties | None,
    ) -> None:
        """Record a disconnect; a later setup refreshes temporary credentials."""
        self._connected.clear()
        self._connected_at = None
        self._needs_credential_refresh = True
        if reason_code != 0:
            _LOGGER.warning(
                "FurryTail MQTT disconnected unexpectedly; paho will reconnect: %s",
                reason_code,
            )
        else:
            _LOGGER.info("FurryTail MQTT disconnected")

    def _handle_message(
        self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage
    ) -> None:
        """Decode a broker message and deliver it from paho's thread."""
        try:
            parsed = parse_mqtt_message(message.payload)
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            _LOGGER.warning("Ignoring invalid FurryTail MQTT update", exc_info=True)
            return
        self._on_update(parsed)
