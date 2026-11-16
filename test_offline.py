"""Offline protocol checks for the FurryTail custom integration."""

from __future__ import annotations

import asyncio
import base64
from collections.abc import Callable
from datetime import datetime, timezone
import importlib.util
import json
from pathlib import Path
import re
import sys
import time
import types
from typing import Any
from urllib.parse import parse_qs, urlsplit

ROOT = Path(__file__).resolve().parent
PACKAGE_PATH = ROOT / "custom_components" / "furrytail"


def _install_package_stubs() -> None:
    """Install package shells without executing the HA integration entrypoint."""
    custom_components = types.ModuleType("custom_components")
    custom_components.__path__ = [str(ROOT / "custom_components")]
    integration = types.ModuleType("custom_components.furrytail")
    integration.__path__ = [str(PACKAGE_PATH)]
    sys.modules.setdefault("custom_components", custom_components)
    sys.modules.setdefault("custom_components.furrytail", integration)

    # The tested modules avoid HA imports, but provide a shell to guarantee the
    # test stays independent of whether Home Assistant is installed.
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    sys.modules.setdefault("homeassistant", homeassistant)

    try:
        import aiohttp  # noqa: F401
    except ImportError:
        aiohttp = types.ModuleType("aiohttp")

        class ClientResponse:
            """Type-only stand-in for the offline import."""

        class ClientSession:
            """Type-only stand-in for the offline import."""

        class ClientError(Exception):
            """Type-only stand-in for the offline import."""

        aiohttp.ClientResponse = ClientResponse
        aiohttp.ClientSession = ClientSession
        aiohttp.ClientError = ClientError
        sys.modules["aiohttp"] = aiohttp

    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError:
        paho = types.ModuleType("paho")
        paho.__path__ = []
        paho_mqtt = types.ModuleType("paho.mqtt")
        paho_mqtt.__path__ = []
        paho_client = types.ModuleType("paho.mqtt.client")
        sys.modules["paho"] = paho
        sys.modules["paho.mqtt"] = paho_mqtt
        sys.modules["paho.mqtt.client"] = paho_client


def _load_module(name: str, filename: str) -> types.ModuleType:
    """Load one integration module from disk under its package name."""
    spec = importlib.util.spec_from_file_location(name, PACKAGE_PATH / filename)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Unable to load {filename}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


class FakeResponse:
    """Small aiohttp response stand-in backed by canned JSON."""

    def __init__(self, payload: Any, status: int = 200) -> None:
        self.payload = payload
        self.status = status
        self.released = False

    async def __aenter__(self) -> FakeResponse:
        return self

    async def __aexit__(self, *args: Any) -> None:
        return None

    def release(self) -> None:
        self.released = True

    def raise_for_status(self) -> None:
        if self.status >= 400:
            raise RuntimeError(f"synthetic HTTP status {self.status}")

    async def json(self, content_type: Any = None) -> Any:
        return self.payload


class FakeSession:
    """Record requests and return sequential or handler-generated responses."""

    def __init__(
        self,
        responses: list[FakeResponse]
        | Callable[[str, str, dict[str, Any]], FakeResponse],
    ) -> None:
        self.responses = responses
        self.requests: list[tuple[str, str, dict[str, Any]]] = []

    async def request(
        self, method: str, url: str, **kwargs: Any
    ) -> FakeResponse:
        self.requests.append((method, url, kwargs))
        if callable(self.responses):
            return self.responses(method, url, kwargs)
        if not self.responses:
            raise AssertionError(f"Unexpected request: {method} {url}")
        return self.responses.pop(0)


def _success(info: Any = None, *, list_value: Any = None) -> FakeResponse:
    """Build a successful Granwin response envelope."""
    payload: dict[str, Any] = {"code": 0}
    if list_value is not None:
        payload["list"] = list_value
    else:
        payload["info"] = info
    return FakeResponse(payload)


def _fake_jwt(payload: dict[str, Any]) -> str:
    """Build a local unsigned test-shaped JWT without storing a token literal."""

    def encode(value: dict[str, Any]) -> str:
        raw = json.dumps(value, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).decode().rstrip("=")

    return ".".join((encode({"alg": "RS256", "typ": "JWT"}), encode(payload), "TEST"))


def _check_token_expiry(const: types.ModuleType, api: types.ModuleType) -> None:
    """Verify JWT expiry parsing and client lifetime derivation."""
    token = _fake_jwt({"iat": 1_900_000_000, "exp": 1_902_592_000})
    assert const.parse_token_expiry(token) == 1_902_592_000
    assert const.parse_token_expiry("TEST_TOKEN") is None
    assert const.parse_token_expiry(_fake_jwt({"iat": 1_900_000_000})) is None
    assert const.parse_token_expiry(_fake_jwt({"exp": "invalid"})) is None

    client = api.FurryTailClient(FakeSession([]), token=token)
    assert client.token_issued_at == 1_900_000_000
    assert client.token_expiration == 2_592_000


async def _check_token_validation(api: types.ModuleType) -> None:
    """Verify raw authorization and validation success versus rejection."""
    token = _fake_jwt({"iat": 1_900_000_000, "exp": 1_902_592_000})
    session = FakeSession([FakeResponse({"code": "0", "info": {"21": "5986"}})])
    client = api.FurryTailClient(session)
    await client.async_validate_token(token, "AA:BB:CC:DD:EE:FF")
    method, url, kwargs = session.requests[0]
    assert (method, url) == (
        "POST",
        "https://app.prod-iot.furrytail.net/device/query/device/property",
    )
    assert kwargs["json"] == {"mac": "AABBCCDDEEFF"}
    assert kwargs["headers"] == {"Authorization": token}

    for response in (
        FakeResponse({"code": "20013", "tip": "TEST_REJECTION"}, status=401),
        FakeResponse({"code": 20013, "tip": "TEST_REJECTION"}),
        FakeResponse({"code": 7, "tip": "TEST_REJECTION"}, status=403),
    ):
        rejected = api.FurryTailClient(FakeSession([response]))
        try:
            await rejected.async_validate_token("TEST_TOKEN", None)
        except api.FurryTailAuthenticationError:
            pass
        else:
            raise AssertionError("Rejected token was accepted")


async def _check_raw_attribute_request(api: types.ModuleType) -> None:
    """Exercise the primary-only property read with a raw token header."""
    session = FakeSession([FakeResponse({"code": "0", "info": {"21": "5986"}})])
    client = api.FurryTailClient(session, token="TEST_TOKEN")
    result = await client.async_get_attributes("AA:BB:CC:DD:EE:FF")
    assert result == {"21": 5986}
    assert session.requests[0][2]["headers"] == {"Authorization": "TEST_TOKEN"}

    failed_session = FakeSession([FakeResponse({"code": 7, "tip": "TEST_FAILURE"})])
    failed_client = api.FurryTailClient(failed_session, token="TEST_TOKEN")
    try:
        await failed_client.async_get_attributes("AA:BB:CC:DD:EE:FF")
    except api.FurryTailApiError:
        pass
    else:
        raise AssertionError("Nonzero property response was accepted")
    assert len(failed_session.requests) == 1


async def _check_refresh_fallback(api: types.ModuleType) -> None:
    """Verify refresh route order, raw headers, and replacement token parsing."""
    old_token = _fake_jwt({"iat": 1_900_000_000, "exp": 1_902_592_000})
    new_token = _fake_jwt({"iat": 1_902_000_000, "exp": 1_904_592_000})
    session = FakeSession(
        [
            FakeResponse({"code": 7, "tip": "TEST_FAILURE"}),
            FakeResponse({"code": "0", "info": {"token": new_token}}),
        ]
    )
    client = api.FurryTailClient(session, token=old_token)
    await client.async_refresh_token()
    assert [request[1] for request in session.requests] == [
        "https://app.prod-iot.furrytail.net/device/user/refresh",
        "https://app.prod-iot.furrytail.net/auth/refresh",
    ]
    assert all(request[2]["json"] == {} for request in session.requests)
    assert all(
        request[2]["headers"] == {"Authorization": old_token}
        for request in session.requests
    )
    assert client.token == new_token
    assert client.token_expiration == 2_592_000

    unchanged_session = FakeSession([FakeResponse({"code": "0", "info": {}})])
    unchanged = api.FurryTailClient(unchanged_session, token="TEST_TOKEN")
    await unchanged.async_refresh_token()
    assert unchanged.token == "TEST_TOKEN"

    string_session = FakeSession(
        [FakeResponse({"code": 0, "info": "TEST_TOKEN_REPLACEMENT"})]
    )
    string_client = api.FurryTailClient(string_session, token="TEST_TOKEN")
    await string_client.async_refresh_token()
    assert string_client.token == "TEST_TOKEN_REPLACEMENT"


async def _check_reactive_rejection(api: types.ModuleType) -> None:
    """Verify terminal reactive rejection preserves the stored token."""
    session = FakeSession(
        [
            FakeResponse({"code": "20013", "tip": "TEST_REJECTION"}, status=401),
            FakeResponse({"code": "0", "info": {}}),
            FakeResponse({"code": 20013, "tip": "TEST_REJECTION"}),
        ]
    )
    client = api.FurryTailClient(session, token="TEST_TOKEN")
    try:
        await client.async_get_attributes("AA:BB:CC:DD:EE:FF")
    except api.FurryTailAuthenticationError:
        pass
    else:
        raise AssertionError("Reactive token rejection was not reported")
    assert client.token == "TEST_TOKEN"


def _check_auth_rejection_cases(api: types.ModuleType) -> None:
    """Verify HTTP status and vendor code authentication classification."""
    check = api.FurryTailClient._is_auth_rejection
    assert not check(FakeResponse({}, status=401), {"code": "20020"})
    assert check(FakeResponse({}, status=401), {"code": 20013})
    assert check(FakeResponse({}, status=401), {})
    assert check(FakeResponse({}, status=401), "TEST_NON_OBJECT")
    assert not check(FakeResponse({}), {"code": "20020"})
    assert check(FakeResponse({}), {"code": 20013})

    try:
        api.FurryTailClient._require_envelope(
            {"code": "20020", "tip": "TEST_PERMISSION"},
            "permission check",
            authentication=True,
        )
    except api.FurryTailApiError:
        pass
    else:
        raise AssertionError("Permission error was accepted as success")


async def _check_proactive_refresh_degradation(api: types.ModuleType) -> None:
    """Verify a failed proactive refresh does not block the current request."""
    expired_token = _fake_jwt({"iat": 1, "exp": 2})
    session = FakeSession(
        [
            FakeResponse({"code": 7, "tip": "TEST_FAILURE"}),
            FakeResponse({"code": 8, "tip": "TEST_FAILURE"}),
            _success({"2": "2"}),
        ]
    )
    client = api.FurryTailClient(session, token=expired_token)
    result = await client.async_get_attributes("AA:BB:CC:DD:EE:FF")
    assert result == {"2": 2}
    assert session.requests[-1][2]["headers"] == {"Authorization": expired_token}


async def _check_discovery_degradation(api: types.ModuleType) -> None:
    """Verify list failures degrade to an empty result in route order."""
    session = FakeSession(
        [
            FakeResponse({"code": 7, "tip": "TEST_FAILURE"}),
            FakeResponse({"code": 8, "tip": "TEST_FAILURE"}),
        ]
    )
    client = api.FurryTailClient(session, token="TEST_TOKEN")
    assert await client.async_list_devices() == []
    assert [request[1] for request in session.requests] == [
        "https://app.prod-iot.furrytail.net/device/user/device/list",
        "https://app.prod-iot.furrytail.net/device/list",
    ]

    permission_session = FakeSession(
        [
            FakeResponse(
                {"code": "20020", "tip": "TEST_PERMISSION"}, status=401
            ),
            FakeResponse(
                {"code": "20020", "tip": "TEST_PERMISSION"}, status=401
            ),
        ]
    )
    permission_client = api.FurryTailClient(
        permission_session, token="TEST_TOKEN"
    )
    assert await permission_client.async_list_devices() == []
    assert permission_client.token == "TEST_TOKEN"
    assert [request[1] for request in permission_session.requests] == [
        "https://app.prod-iot.furrytail.net/device/user/device/list",
        "https://app.prod-iot.furrytail.net/device/list",
    ]


async def _check_http_control(api: types.ModuleType) -> None:
    """Verify fixed data type 81 and the HTTPS control variant cascade."""
    session = FakeSession([FakeResponse({"code": "0", "info": {}})])
    client = api.FurryTailClient(
        session,
        token="TEST_TOKEN",
        mac="AA:BB:CC:DD:EE:FF",
    )
    assert await client.async_resolve_data_type(3) == 81
    await client.async_set_attribute_http(3, 1)
    method, url, kwargs = session.requests[0]
    assert (method, url) == (
        "POST",
        "https://app.prod-iot.furrytail.net/device/control/device",
    )
    assert kwargs["headers"]["Authorization"] == "TEST_TOKEN"
    assert kwargs["json"]["mac"] == "AABBCCDDEEFF"
    assert "placeId" not in kwargs["json"]
    assert "merchantId" not in kwargs["json"]
    # The HTTP fallback uses a PLAIN property map (the typed
    # {"type", "value"} envelope is the MQTT payload shape only).
    assert kwargs["json"]["propertyMap"] == {"3": 1}

    # A rejection cascades to the context variant, which adds the
    # merchantId and (when known) the placeId to the same plain body.
    cascade_session = FakeSession(
        [
            FakeResponse({"message": "Unauthorized"}, status=401),
            FakeResponse({"code": "0", "info": {}}),
        ]
    )
    cascade_client = api.FurryTailClient(
        cascade_session,
        token="TEST_TOKEN",
        mac="AA:BB:CC:DD:EE:FF",
        place_id="1234567890123456789",
    )
    await cascade_client.async_set_attribute_http(3, 1)
    assert len(cascade_session.requests) == 2
    first, second = cascade_session.requests
    assert first[1] == second[1] == (
        "https://app.prod-iot.furrytail.net/device/control/device"
    )
    assert "placeId" not in first[2]["json"]
    assert second[2]["json"]["placeId"] == "1234567890123456789"
    assert second[2]["json"]["merchantId"] == "100000000000000000"
    assert second[2]["json"]["propertyMap"] == {"3": 1}
    assert all(
        request[2]["headers"]["Authorization"] == "TEST_TOKEN"
        for request in cascade_session.requests
    )


async def _check_cognito_exchange(api: types.ModuleType) -> None:
    """Verify the unsigned Cognito developer-auth exchange request shape."""
    session = FakeSession(
        [
            FakeResponse(
                {
                    "Credentials": {
                        "AccessKeyId": "FAKE_ACCESS_KEY",
                        "SecretKey": "FAKE_SECRET_KEY",
                        "SessionToken": "FAKE_SESSION_TOKEN",
                        "Expiration": 1900000000,
                    }
                }
            )
        ]
    )
    client = api.FurryTailClient(session)
    credentials = await client.async_exchange_cognito_credentials(
        {
            "region": "us-test-1",
            "identity_id": "us-test-1:synthetic-identity",
            "token": "synthetic-developer-token",
        }
    )
    method, url, kwargs = session.requests[0]
    assert (method, url) == (
        "POST",
        "https://cognito-identity.us-test-1.amazonaws.com/",
    )
    assert kwargs["headers"] == {
        "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
        "Content-Type": "application/x-amz-json-1.1",
    }
    assert kwargs["json"] == {
        "IdentityId": "us-test-1:synthetic-identity",
        "Logins": {"granwin": "synthetic-developer-token"},
    }
    assert credentials == {
        "access_key_id": "FAKE_ACCESS_KEY",
        "secret_access_key": "FAKE_SECRET_KEY",
        "session_token": "FAKE_SESSION_TOKEN",
        "expiration": 1900000000,
    }


async def _check_aws_policy_normalization(api: types.ModuleType) -> None:
    """Verify STS, Cognito, and incomplete AWS Policy normalization."""
    sts_session = FakeSession(
        [
            _success(
                {
                    "endpoint": "synthetic.iot.us-test-1.amazonaws.com",
                    "region": "us-test-1",
                    "clientId": "synthetic-client",
                    "credentials": {
                        "accessKeyId": "FAKE_ACCESS_KEY",
                        "secretAccessKey": "FAKE_SECRET_KEY",
                        "sessionToken": "FAKE_SESSION_TOKEN",
                        "expiration": 1900000000,
                    },
                }
            )
        ]
    )
    sts_client = api.FurryTailClient(sts_session, token="TEST_TOKEN")
    sts = await sts_client.async_get_aws_credentials()
    assert sts["mode"] == "sts"
    assert sts["endpoint"] == "synthetic.iot.us-test-1.amazonaws.com"
    assert sts["credentials_expiration"] == 1900000000

    cognito_session = FakeSession(
        [
            _success(
                {
                    "iotEndpoint": "https://synthetic.iot.us-test-1.amazonaws.com/mqtt",
                    "region": "us-test-1",
                    "identityId": "us-test-1:synthetic-identity",
                    "token": "synthetic-developer-token",
                    "identityPoolId": "us-test-1:synthetic-pool",
                }
            )
        ]
    )
    cognito_client = api.FurryTailClient(
        cognito_session, token="TEST_TOKEN"
    )
    cognito = await cognito_client.async_get_aws_credentials()
    assert cognito["mode"] == "cognito"
    assert cognito["client_id"] == "us-test-1:synthetic-identity"
    assert cognito["credentials"] is None

    incomplete_session = FakeSession(
        [_success({"identityId": "synthetic-identity", "token": "synthetic-token"})]
    )
    incomplete_client = api.FurryTailClient(
        incomplete_session, token="TEST_TOKEN"
    )
    incomplete = await incomplete_client.async_get_aws_credentials()
    assert incomplete["mode"] is None

    failed_session = FakeSession(
        [
            FakeResponse({"code": 7, "tip": "TEST_FAILURE"}),
            FakeResponse({"code": 8, "tip": "TEST_FAILURE"}),
            FakeResponse({"code": 9, "tip": "TEST_FAILURE"}),
        ]
    )
    failed_client = api.FurryTailClient(failed_session, token="TEST_TOKEN")
    failed = await failed_client.async_get_aws_credentials()
    assert failed["mode"] is None
    assert [request[1] for request in failed_session.requests] == [
        "https://app.prod-iot.furrytail.net/device/add/policy",
        "https://app.prod-iot.furrytail.net/device/user/client/generate",
        "https://app.prod-iot.furrytail.net/device/client/generate",
    ]

    permission_session = FakeSession(
        [
            FakeResponse(
                {"code": "20020", "tip": "TEST_PERMISSION"}, status=401
            ),
            FakeResponse(
                {"code": "20020", "tip": "TEST_PERMISSION"}, status=401
            ),
            FakeResponse(
                {"code": "20020", "tip": "TEST_PERMISSION"}, status=401
            ),
        ]
    )
    permission_client = api.FurryTailClient(
        permission_session, token="TEST_TOKEN"
    )
    permission = await permission_client.async_get_aws_credentials()
    assert permission["mode"] is None
    assert permission_client.token == "TEST_TOKEN"
    assert [request[1] for request in permission_session.requests] == [
        "https://app.prod-iot.furrytail.net/device/add/policy",
        "https://app.prod-iot.furrytail.net/device/user/client/generate",
        "https://app.prod-iot.furrytail.net/device/client/generate",
    ]


async def _check_api(api: types.ModuleType) -> None:
    """Run every async API assertion."""
    await _check_token_validation(api)
    await _check_raw_attribute_request(api)
    await _check_refresh_fallback(api)
    await _check_reactive_rejection(api)
    await _check_proactive_refresh_degradation(api)
    await _check_discovery_degradation(api)
    await _check_http_control(api)
    await _check_cognito_exchange(api)
    await _check_aws_policy_normalization(api)


def _check_sigv4(mqtt_client: types.ModuleType) -> None:
    """Verify SigV4 shape, determinism, token signing, and key sensitivity."""
    fixed_now = datetime(2030, 1, 2, 3, 4, 5, tzinfo=timezone.utc)
    args = (
        "synthetic.iot.us-test-1.amazonaws.com",
        "us-test-1",
        "FAKE_ACCESS_KEY",
        "FAKE_SECRET_KEY_A",
        "FAKE_SESSION_TOKEN/value+with=symbols",
    )
    first = mqtt_client.build_aws_iot_wss_path(*args, now=fixed_now)
    second = mqtt_client.build_aws_iot_wss_path(*args, now=fixed_now)
    changed = mqtt_client.build_aws_iot_wss_path(
        args[0], args[1], args[2], "FAKE_SECRET_KEY_B", args[4], now=fixed_now
    )
    assert first == second
    assert first.startswith("/mqtt?")
    query = parse_qs(urlsplit(first).query)
    assert query["X-Amz-Algorithm"] == ["AWS4-HMAC-SHA256"]
    assert query["X-Amz-Credential"] == [
        "FAKE_ACCESS_KEY/20300102/us-test-1/iotdata/aws4_request"
    ]
    assert query["X-Amz-Date"] == ["20300102T030405Z"]
    assert query["X-Amz-SignedHeaders"] == ["host"]
    assert query["X-Amz-Security-Token"] == [args[4]]
    assert re.fullmatch(r"[0-9a-f]{64}", query["X-Amz-Signature"][0])
    changed_query = parse_qs(urlsplit(changed).query)
    assert changed_query["X-Amz-Signature"] != query["X-Amz-Signature"]


def main() -> None:
    """Run all checks without opening sockets or creating real client sessions."""
    _install_package_stubs()
    const = _load_module("custom_components.furrytail.const", "const.py")
    api = _load_module("custom_components.furrytail.api", "api.py")
    mqtt_client = _load_module(
        "custom_components.furrytail.mqtt_client", "mqtt_client.py"
    )

    raw: dict[str, Any] = {
        "1": "0",
        "2": "0",
        "3": "0",
        "4": "0",
        "5": "0",
        "6": "1",
        "8": "1",
        "9": "0",
        "10": "0",
        "11": "",
        "12": "00",
        "14": "0",
        "15": "16000800",
        "16": "1",
        "17": "00000A00",
        "18": "1",
        "19": "0",
        "20": "87",
        "21": "5986",
        "22": "0",
        "23": "0",
        "24": "1",
        "25": "true",
        "26": "true",
        "27": "true",
        "28": "true",
        "29": "0",
        "30": "1",
        "31": "",
        "32": "",
        "33": "148",
        "onlineStatus": True,
    }
    parsed = const.parse_attributes(raw)
    assert parsed["1"] == 0
    assert parsed["6"] == 1
    assert parsed["11"] is None
    assert parsed["12"] == 0
    assert parsed["17"] == "00000A00"
    assert parsed["25"] is True
    assert parsed["31"] is None
    assert parsed["33"] == 148
    assert parsed["onlineStatus"] is True
    assert "7" not in parsed and "13" not in parsed
    assert const.parse_attributes({"future": "value"}) == {"future": "value"}

    before = int(time.time())
    control = const.build_control_payload(3, 1)
    typed_control = const.build_control_payload(4, 1, data_type=65)
    after = int(time.time())
    assert control["data"] == {"3": {"type": 81, "value": 1}}
    assert typed_control["data"] == {"4": {"type": 65, "value": 1}}
    assert before <= control["time"] <= after

    assert const.build_update_topic(
        "0123456789abcdef", "AA:BB:CC:DD:EE:FF"
    ) == "/0123456789abcdef/AABBCCDDEEFF/user/update"
    assert const.build_control_topic(
        "0123456789abcdef", "AA:BB:CC:DD:EE:FF"
    ) == "/0123456789abcdef/AABBCCDDEEFF/user/get"

    value_update = mqtt_client.parse_mqtt_message(
        b'{"value":{"2":{"type":81,"value":2}}}'
    )
    data_update = mqtt_client.parse_mqtt_message(
        b'{"data":{"2":{"type":81,"value":2}}}'
    )
    both_update = mqtt_client.parse_mqtt_message(
        b'{"value":{"2":{"type":81,"value":1}},'
        b'"data":{"2":{"type":81,"value":2},"3":{"value":1}}}'
    )
    assert value_update == {"value": {"2": 2}}
    assert data_update == {"data": {"2": 2}}
    assert both_update == {"value": {"2": 1}, "data": {"2": 2, "3": 1}}
    merged = {**both_update["value"], **both_update["data"]}
    assert merged == {"2": 2, "3": 1}

    assert const.grams_to_kilograms(parsed["21"]) == 5.986
    assert const.token_refresh_leeway(3600) == 300
    assert const.token_refresh_leeway(20) == 2
    _check_token_expiry(const, api)
    _check_auth_rejection_cases(api)
    _check_sigv4(mqtt_client)
    asyncio.run(_check_api(api))

    print("offline tests passed")


if __name__ == "__main__":
    main()
