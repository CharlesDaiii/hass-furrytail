"""Constants and protocol helpers for the FurryTail integration."""

from __future__ import annotations

import base64
import binascii
import json
import time
from typing import Any

DOMAIN = "furrytail"
BASE_URL = "https://app.prod-iot.furrytail.net"
DEFAULT_SCAN_INTERVAL = 60

CONF_MAC = "mac"
CONF_TOKEN = "token"
# ``account_id`` was incorrectly used as the MQTT topic prefix in 0.1.0.
# Keep it only so existing entries can be read and migrated in place.
CONF_ACCOUNT_ID = "account_id"
CONF_PRODUCT_KEY = "product_key"
CONF_DEVICE_ID = "device_id"
CONF_THING_NAME = "thing_name"
CONF_PLACE_ID = "place_id"
CONF_SCAN_INTERVAL = "scan_interval"

DEFAULT_MAC = ""
DEFAULT_PRODUCT_KEY = "0002f3c7d847ce72"

# Granwin platform merchant ID for the FurryTail app (吾尾物联网平台).
# A fixed constant baked into the FurryTail Home app (verified in the
# v1.0.12 APK: LoginApi and 30 sibling request classes hardcode it);
# the same for every user, NOT a credential. The /device/* backend
# rejects requests without it ("Merchant ID cannot be empty!").
MERCHANT_ID = "100000000000000000"

ATTR_CAT_STATUS = 1
ATTR_DEVICE_STATUS = 2
ATTR_MANUAL_CLEAN_STATUS = 3
ATTR_FLATTEN_STATUS = 4
ATTR_SAND_CHANGING_STATUS = 5
ATTR_IS_AUTO_CLEAN = 6
ATTR_AUTO_CLEAN_STATUS = 7
ATTR_AUTO_CLEAN_TIMER = 8
ATTR_IS_POOP_STORAGE_REMINDER = 9
ATTR_POOP_STORAGE_USAGE = 10
ATTR_POOP_STORAGE_RESET_STATUS = 11
ATTR_CLEAN_SCHEDULE = 12
ATTR_CLEAN_SCHEDULE_STATUS = 13
ATTR_IS_DO_NOT_DISTURB = 14
ATTR_DO_NOT_DISTURB_DURATION = 15
ATTR_IS_INDICATOR_LIGHT_ON = 16
ATTR_INDICATOR_LIGHT_DURATION = 17
ATTR_IS_CLEAN_SHAKE_ON = 18
ATTR_IS_CHILD_LOCK_ON = 19
ATTR_STAY_INTERVAL = 20
ATTR_CAT_WEIGHT = 21
ATTR_LIGHT_BRIGHTNESS = 22
ATTR_DEVICE_FAULT = 23
ATTR_TASK_TYPE = 24
ATTR_NOTIFICATION_SWITCH_25 = 25
ATTR_NOTIFICATION_SWITCH_26 = 26
ATTR_NOTIFICATION_SWITCH_27 = 27
ATTR_NOTIFICATION_SWITCH_28 = 28
ATTR_ROLLER_RESET_STATUS = 29
ATTR_WEIGHT_UNIT = 30
ATTR_PET_ID = 31
ATTR_PET_NAME = 32
ATTR_POOP_COUNT = 33
ATTR_ONLINE_STATUS = "onlineStatus"

# Only values whose meanings are supported by observed protocol data are named.
DEVICE_STATUS: dict[int, str] = {
    0: "idle",
    2: "cleaning",
}
TASK_TYPE: dict[int, str] = {
    1: "task 1",
}


def normalize_mac(mac: str) -> str:
    """Return a MAC in the compact uppercase form used by the cloud API."""
    return mac.replace(":", "").replace("-", "").upper()


def parse_attribute_value(value: Any) -> Any:
    """Decode one vendor attribute value without rejecting unknown types."""
    if not isinstance(value, str):
        return value

    stripped = value.strip()
    if stripped == "":
        return None

    lowered = stripped.lower()
    if lowered == "true":
        return True
    if lowered == "false":
        return False

    try:
        return int(stripped)
    except ValueError:
        return value


def parse_attributes(raw: dict[Any, Any]) -> dict[str, Any]:
    """Decode a possibly partial vendor attribute map.

    Missing known attributes are intentionally left absent so entities expose
    them as unknown. Unknown attributes are retained for forward compatibility.
    """
    return {str(key): parse_attribute_value(value) for key, value in raw.items()}


def grams_to_kilograms(value: Any) -> float | None:
    """Convert a numeric vendor weight in grams to kilograms."""
    if value is None:
        return None
    try:
        return float(value) / 1000
    except (TypeError, ValueError):
        return None


def token_refresh_leeway(lifetime: int) -> int:
    """Return the Granwin SDK refresh leeway for a token lifetime."""
    return min(300, max(1, lifetime // 10))


def _parse_token_payload(token: str) -> dict[str, Any] | None:
    """Decode a JWT payload without validating its signature."""
    try:
        payload_segment = token.split(".")[1]
        padding = "=" * (-len(payload_segment) % 4)
        payload = json.loads(
            base64.urlsafe_b64decode(payload_segment + padding).decode("utf-8")
        )
    except (
        AttributeError,
        binascii.Error,
        IndexError,
        TypeError,
        UnicodeDecodeError,
        ValueError,
    ):
        return None
    return payload if isinstance(payload, dict) else None


def parse_token_expiry(token: str) -> float | None:
    """Return a JWT expiration epoch, or None when the token cannot be parsed."""
    payload = _parse_token_payload(token)
    if payload is None:
        return None
    try:
        return float(payload["exp"])
    except (KeyError, TypeError, ValueError):
        return None


def _topic_thing_name(thing_name: str | None, mac: str | None = None) -> str:
    """Return the normalized MQTT thing name, falling back to the MAC."""
    candidate = thing_name or mac or ""
    return normalize_mac(candidate.strip())


def build_update_topic(
    product_key: str, thing_name: str | None, mac: str | None = None
) -> str:
    """Build the SDK-form device update topic with a leading slash."""
    return f"/{product_key.strip()}/{_topic_thing_name(thing_name, mac)}/user/update"


def build_control_topic(
    product_key: str, thing_name: str | None, mac: str | None = None
) -> str:
    """Build the SDK-form device control topic with a leading slash."""
    return f"/{product_key.strip()}/{_topic_thing_name(thing_name, mac)}/user/get"


def build_property_query_topic(
    product_key: str, thing_name: str | None, mac: str | None = None
) -> str:
    """Build the SDK-form MQTT property-query request topic."""
    return (
        f"/{product_key.strip()}/{_topic_thing_name(thing_name, mac)}"
        "/user/property/query"
    )


def build_property_query_response_topic(
    product_key: str, thing_name: str | None, mac: str | None = None
) -> str:
    """Build the SDK-form MQTT property-query response topic."""
    return (
        f"/{product_key.strip()}/{_topic_thing_name(thing_name, mac)}"
        "/user/property/query_response"
    )


def build_control_payload(
    attr_id: int, value: Any, data_type: int = 81
) -> dict[str, Any]:
    """Build the common Granwin attribute-control payload."""
    return {
        "data": {str(attr_id): {"type": data_type, "value": value}},
        "time": int(time.time()),
    }
