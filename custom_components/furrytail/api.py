"""Async cloud API client for FurryTail devices."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from aiohttp import ClientError, ClientResponse, ClientSession

from .const import (
    BASE_URL,
    DEFAULT_MAC,
    DEFAULT_PRODUCT_KEY,
    MERCHANT_ID,
    _parse_token_payload,
    build_control_payload,
    normalize_mac,
    parse_attributes,
    parse_token_expiry,
    token_refresh_leeway,
)

_LOGGER = logging.getLogger(__name__)

AUTH_HEADER = "Authorization"
ATTRIBUTE_PATH = "/device/query/device/property"
REFRESH_PATHS = ("/device/user/refresh", "/auth/refresh")
DEVICE_LIST_PATHS = ("/device/user/device/list", "/device/list")
AWS_POLICY_PATHS = (
    "/device/add/policy",
    "/device/user/client/generate",
    "/device/client/generate",
)
HTTP_CONTROL_PATH = "/device/control/device"


class FurryTailAuthenticationError(Exception):
    """Raised when the cloud rejects FurryTail credentials."""


class FurryTailApiError(Exception):
    """Raised when the cloud returns a valid but unsuccessful response."""


class FurryTailClient:
    """Async client for the Granwin-backed FurryTail cloud protocol."""

    def __init__(
        self,
        session: ClientSession,
        token: str | None = None,
        account_id: str | None = None,
        mac: str | None = None,
        *,
        product_key: str | None = None,
        device_id: str | None = None,
        thing_name: str | None = None,
        place_id: str | None = None,
    ) -> None:
        """Initialize the client without making a network request."""
        self._session = session
        self._token: str | None = None
        self.token_expiration = 0
        self.token_issued_at = 0.0

        # ``account_id`` is accepted only for 0.1.0 callers/config entries.
        self.account_id = account_id
        self.mac = normalize_mac(mac) if mac else None
        self.product_key = product_key or account_id or DEFAULT_PRODUCT_KEY
        self.device_id = device_id
        self.product_id: str | None = None
        self.thing_name = normalize_mac(thing_name) if thing_name else None

        self.place_id = str(place_id) if place_id else None

        self.devices: list[dict[str, Any]] = []
        self._control_route: str | None = None
        if token:
            self._store_token(token)

    @property
    def token(self) -> str | None:
        """Return the current access token for diagnostics and tests."""
        return self._token

    def _auth_headers(self) -> dict[str, str]:
        """Build the vendor's raw-token authorization header."""
        return {AUTH_HEADER: self._token} if self._token else {}

    def _store_token(self, token: str) -> None:
        """Store a token and derive its lifetime from its JWT claims."""
        self._token = token
        now = time.time()
        payload = _parse_token_payload(token)
        issued_at = now
        if payload is not None:
            _LOGGER.info(
                "FurryTail token claim keys: %s", sorted(payload.keys())
            )
            if self.place_id is None:
                for key in (
                    "placeId",
                    "place_id",
                    "currentPlaceId",
                    "homePlaceId",
                    "placeID",
                ):
                    if payload.get(key):
                        self.place_id = str(payload[key])
                        break
            try:
                issued_at = float(payload["iat"])
            except (KeyError, TypeError, ValueError):
                pass
        expiry = parse_token_expiry(token)
        self.token_issued_at = issued_at
        self.token_expiration = (
            expiry - issued_at if expiry is not None else 0
        )

    def _token_needs_refresh(self) -> bool:
        """Return whether the SDK refresh threshold has been reached."""
        if (
            not self._token
            or self.token_expiration <= 0
            or self.token_issued_at <= 0
        ):
            return False
        leeway = token_refresh_leeway(int(self.token_expiration))
        return time.time() >= (
            self.token_issued_at + self.token_expiration - leeway
        )

    async def _raw_request(
        self, method: str, path: str, **kwargs: Any
    ) -> ClientResponse:
        """Issue one request without refresh logic or implicit auth headers."""
        return await self._session.request(method, f"{BASE_URL}{path}", **kwargs)

    @staticmethod
    async def _response_payload(response: ClientResponse) -> Any:
        """Read a JSON response while allowing empty/non-JSON error bodies."""
        try:
            return await response.json(content_type=None)
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _payload_code(payload: Any) -> str | None:
        """Return a response code as text, or None when it is absent."""
        if not isinstance(payload, dict) or "code" not in payload:
            return None
        return str(payload["code"])

    @staticmethod
    def _is_auth_rejection(response: ClientResponse, payload: Any) -> bool:
        """Recognize the HTTP and Granwin business authentication errors."""
        code = FurryTailClient._payload_code(payload)
        if getattr(response, "status", 200) == 401:
            return code is None or code in {"10004", "20013"}
        return code in {"10004", "20013"}

    @staticmethod
    def _release_response(response: ClientResponse) -> None:
        """Release a response before retrying when the implementation supports it."""
        release = getattr(response, "release", None)
        if callable(release):
            release()

    async def _request(
        self, method: str, path: str, **kwargs: Any
    ) -> ClientResponse:
        """Make an authenticated request with one refresh-and-retry attempt."""
        if self._token_needs_refresh():
            try:
                await self.async_refresh_token()
            except FurryTailAuthenticationError as err:
                _LOGGER.warning(
                    "Proactive FurryTail token refresh failed; continuing with "
                    "the current token: %s",
                    err,
                )

        original_headers = dict(kwargs.pop("headers", {}))

        async def issue() -> ClientResponse:
            headers = dict(original_headers)
            headers.update(self._auth_headers())
            return await self._raw_request(method, path, headers=headers, **kwargs)

        response = await issue()
        payload = await self._response_payload(response)
        if not self._is_auth_rejection(response, payload):
            return response

        self._release_response(response)
        await self.async_refresh_token()

        response = await issue()
        payload = await self._response_payload(response)
        if self._is_auth_rejection(response, payload):
            self._release_response(response)
            raise FurryTailAuthenticationError(
                "FurryTail rejected the refreshed access token"
            )
        return response

    @staticmethod
    def _require_envelope(
        payload: Any, operation: str, *, authentication: bool = False
    ) -> dict[str, Any]:
        """Validate a Granwin response envelope and return it."""
        if not isinstance(payload, dict):
            raise FurryTailApiError(
                f"FurryTail {operation} returned a non-object response"
            )
        code = FurryTailClient._payload_code(payload)
        if code != "0":
            detail = payload.get("tip") or payload.get("message") or "vendor error"
            error = f"FurryTail {operation} failed: {detail}"
            if code in {"10004", "20013"}:
                raise FurryTailAuthenticationError(error)
            raise FurryTailApiError(error)
        return payload

    @staticmethod
    def _extract_list(payload: dict[str, Any]) -> list[Any]:
        """Extract either BaseHttpListResponse.list or a list-valued info."""
        result = payload.get("list")
        if isinstance(result, list):
            return result
        info = payload.get("info")
        if isinstance(info, list):
            return info
        if isinstance(info, dict):
            for key in ("list", "records", "data"):
                nested = info.get(key)
                if isinstance(nested, list):
                    return nested
        return []

    async def async_validate_token(self, token: str, mac: str | None = None) -> None:
        """Validate an app token against the proven property endpoint."""
        self._store_token(token)
        normalized_mac = normalize_mac(mac or DEFAULT_MAC)
        self.mac = normalized_mac
        response = await self._raw_request(
            "POST",
            ATTRIBUTE_PATH,
            headers=self._auth_headers(),
            json={"mac": normalized_mac},
        )
        async with response:
            payload = await self._response_payload(response)
            status = getattr(response, "status", 200)
            if status == 403 or self._is_auth_rejection(response, payload):
                raise FurryTailAuthenticationError(
                    "FurryTail rejected the supplied access token"
                )
            if status != 200:
                response.raise_for_status()
                raise FurryTailApiError(
                    f"FurryTail token validation returned HTTP {status}"
                )
        self._require_envelope(payload, "token validation")

    @staticmethod
    def _refresh_response_token(payload: dict[str, Any]) -> str | None:
        """Extract a replacement token from the known refresh shapes."""
        info = payload.get("info")
        if isinstance(info, dict) and info.get("token"):
            return str(info["token"])
        if payload.get("token"):
            return str(payload["token"])
        if isinstance(info, str) and info:
            return info
        return None

    async def async_refresh_token(self) -> None:
        """Try the live refresh routes using the current access token."""
        if not self._token:
            raise FurryTailAuthenticationError("No FurryTail access token is stored")

        last_error: Exception | None = None
        for path in REFRESH_PATHS:
            try:
                response = await self._raw_request(
                    "POST", path, headers=self._auth_headers(), json={}
                )
                async with response:
                    payload = await self._response_payload(response)
                    if self._is_auth_rejection(response, payload):
                        raise FurryTailAuthenticationError(
                            "FurryTail rejected the token refresh request"
                        )
                    response.raise_for_status()
                envelope = self._require_envelope(payload, "token refresh")
                refreshed_token = self._refresh_response_token(envelope)
                if refreshed_token:
                    self._store_token(refreshed_token)
                return
            except Exception as err:
                last_error = err
                _LOGGER.debug(
                    "FurryTail token refresh path %s failed: %s", path, err
                )

        raise FurryTailAuthenticationError(
            "All FurryTail token refresh paths failed"
        ) from last_error

    @staticmethod
    def _first_value(item: dict[str, Any], *keys: str) -> Any:
        """Return the first non-empty value from a set of aliases."""
        for key in keys:
            value = item.get(key)
            if value is not None and value != "":
                return value
        return None

    async def async_list_devices(self) -> list[dict[str, Any]]:
        """Fetch devices, normalize aliases, and retain them on the client."""
        envelope: dict[str, Any] | None = None
        failures: list[str] = []
        list_body: dict[str, Any] = {"merchantId": MERCHANT_ID}
        if self.place_id:
            list_body["placeId"] = self.place_id
        for path in DEVICE_LIST_PATHS:
            try:
                response = await self._request("POST", path, json=list_body)
                async with response:
                    response.raise_for_status()
                    payload = await self._response_payload(response)
                envelope = self._require_envelope(payload, "device list")
                break
            except Exception as err:
                failures.append(f"{path}: {err}")
                _LOGGER.debug("FurryTail device list path %s failed", path, exc_info=True)

        if envelope is None:
            _LOGGER.warning(
                "FurryTail device discovery failed on all known paths; "
                "continuing with stored device details: %s",
                "; ".join(failures),
            )
            self.devices = []
            return []

        devices: list[dict[str, Any]] = []
        for raw in self._extract_list(envelope):
            if not isinstance(raw, dict):
                continue
            mac = self._first_value(raw, "mac")
            product_id = self._first_value(raw, "productId", "product_id")
            device = {
                "device_id": self._first_value(raw, "deviceId", "device_id", "id"),
                "mac": normalize_mac(str(mac)) if mac else "",
                "thing_name": self._first_value(
                    raw, "deviceName", "device_name", "thingName"
                ),
                "nickname": self._first_value(raw, "deviceNickName", "nickname"),
                "online": self._first_value(raw, "onlineStatus", "online"),
                "product_key": self._first_value(raw, "productKey", "product_key"),
                "product_id": str(product_id) if product_id is not None else None,
            }
            devices.append(device)

        self.devices = devices
        self.select_device(self.mac)
        return devices

    def select_device(self, mac: str | None = None) -> dict[str, Any] | None:
        """Select a normalized-MAC match, or the first device when no MAC is set."""
        selected: dict[str, Any] | None = None
        if mac:
            wanted = normalize_mac(mac)
            selected = next(
                (device for device in self.devices if device.get("mac") == wanted),
                None,
            )
        elif self.devices:
            selected = self.devices[0]

        if selected is None:
            return None

        self.mac = selected.get("mac") or self.mac
        self.device_id = selected.get("device_id") or self.device_id
        self.product_id = selected.get("product_id") or self.product_id
        product_key = selected.get("product_key")
        self.product_key = product_key or self.product_key or DEFAULT_PRODUCT_KEY
        thing_name = selected.get("thing_name") or self.mac
        self.thing_name = normalize_mac(str(thing_name)) if thing_name else None
        return selected

    async def async_resolve_data_type(self, attr_id: int | str) -> int:
        """Return the observed Granwin control data type."""
        _LOGGER.debug("Using FurryTail data type 81 for attribute %s", attr_id)
        return 81

    async def async_get_attributes(self, mac: str) -> dict[str, Any]:
        """Fetch attributes from the proven MAC property endpoint."""
        normalized_mac = normalize_mac(mac)
        self.mac = normalized_mac or self.mac
        response = await self._request(
            "POST", ATTRIBUTE_PATH, json={"mac": normalized_mac}
        )
        async with response:
            response.raise_for_status()
            payload = await self._response_payload(response)
        envelope = self._require_envelope(payload, "attribute request")
        raw_attributes = envelope.get("info")
        if not isinstance(raw_attributes, dict):
            raise FurryTailApiError(
                "FurryTail attribute response did not contain an info object"
            )
        return parse_attributes(raw_attributes)

    async def async_set_attribute_http(
        self, attr_id: int, value: Any, data_type: int | None = None
    ) -> None:
        """Set an attribute through the SDK's HTTPS control endpoint."""
        if data_type is None:
            try:
                data_type = await self.async_resolve_data_type(attr_id)
            except Exception:
                _LOGGER.warning(
                    "Unable to resolve FurryTail data type for attribute %s; "
                    "using compatibility fallback 81",
                    attr_id,
                    exc_info=True,
                )
                data_type = 81
        # The app's HTTP control fallback, verified by MITM capture of the
        # real app (and reproduced by an independent integration):
        # POST /device/control/device with a PLAIN property map
        # {"mac": ..., "propertyMap": {"3": 1}}. Two earlier mistakes cost
        # a long evening: the path is /device/control/device (not
        # /device/control, which authenticates but always answers "user
        # has no permission for this operation"), and the typed
        # {"data": {id: {type, value}}, "time"} envelope is the MQTT
        # payload shape, not the HTTP one. Context fields (placeId,
        # merchantId) are still sent as a second variant in case a given
        # backend build wants them; the typed shape is kept as a last
        # fallback. A variant counts as successful only when the response
        # envelope reports code 0. A transport error (no response) aborts
        # the cascade because the command may already have been delivered.
        plain_map = {str(attr_id): value}
        typed_map = build_control_payload(attr_id, value, data_type)
        variants: list[tuple[str, dict[str, Any], dict[str, str]]] = []
        if self.mac:
            mac_body: dict[str, Any] = {
                "mac": normalize_mac(self.mac),
                "propertyMap": plain_map,
            }
            variants.append(("plain", mac_body, {}))
            context_body = {**mac_body, "merchantId": MERCHANT_ID}
            if self.place_id:
                context_body["placeId"] = self.place_id
            variants.append(("plain+context", context_body, {}))
            variants.append(
                ("typed", {**mac_body, "propertyMap": typed_map}, {})
            )
        if self.device_id:
            variants.append(
                (
                    "deviceId-plain",
                    {"deviceId": self.device_id, "propertyMap": plain_map},
                    {},
                )
            )
        if not variants:
            raise FurryTailApiError(
                "FurryTail HTTPS control requires a MAC or deviceId"
            )
        if self._control_route is not None:
            labels = [label for label, _, _ in variants]
            if self._control_route in labels:
                index = labels.index(self._control_route)
                variants.insert(0, variants.pop(index))

        failures: list[str] = []
        for label, body, extra_headers in variants:
            headers = dict(self._auth_headers())
            headers.update(extra_headers)
            try:
                response = await self._raw_request(
                    "POST", HTTP_CONTROL_PATH, json=body, headers=headers
                )
            except (ClientError, asyncio.TimeoutError) as err:
                raise FurryTailApiError(
                    f"FurryTail HTTPS control transport error via {label}: {err}"
                ) from err
            async with response:
                status = response.status
                result = await self._response_payload(response)
            try:
                self._require_envelope(result, "HTTPS control")
            except (FurryTailApiError, FurryTailAuthenticationError) as err:
                failures.append(f"{label}: HTTP {status} ({err})")
                _LOGGER.warning(
                    "FurryTail control attempt failed: %s", failures[-1]
                )
                continue
            self._control_route = label
            _LOGGER.info(
                "FurryTail control succeeded via %s (HTTP %s)", label, status
            )
            return
        raise FurryTailApiError(
            "FurryTail HTTPS control failed on all variants: "
            + "; ".join(failures)
        )

    @staticmethod
    def _normalize_endpoint(endpoint: Any) -> str | None:
        """Normalize an AWS IoT endpoint to a bare host name."""
        if not endpoint:
            return None
        host = str(endpoint).strip()
        if "://" in host:
            host = host.split("://", 1)[1]
        host = host.split("/", 1)[0]
        return host or None

    async def async_get_aws_credentials(self) -> dict[str, Any]:
        """Fetch AWS IoT data, degrading to polling when routes are unavailable."""
        info: dict[str, Any] | None = None
        failures: list[str] = []
        aws_body: dict[str, Any] = {"merchantId": MERCHANT_ID}
        if self.place_id:
            aws_body["placeId"] = self.place_id
        for path in AWS_POLICY_PATHS:
            try:
                response = await self._request("POST", path, json=aws_body)
                async with response:
                    response.raise_for_status()
                    payload = await self._response_payload(response)
                envelope = self._require_envelope(payload, "AWS client policy")
                candidate = envelope.get("info")
                if not isinstance(candidate, dict):
                    raise FurryTailApiError(
                        "FurryTail AWS policy response did not contain an info object"
                    )
                info = candidate
                break
            except Exception as err:
                failures.append(f"{path}: {err}")
                _LOGGER.debug("FurryTail AWS policy path %s failed", path, exc_info=True)

        if info is None:
            _LOGGER.warning(
                "FurryTail AWS policy generation failed on all known paths; "
                "continuing in polling-only mode: %s",
                "; ".join(failures),
            )
            return {
                "mode": None,
                "endpoint": None,
                "region": None,
                "client_id": None,
                "identity_id": None,
                "token": None,
                "identity_pool_id": None,
                "credentials": None,
                "credentials_expiration": None,
                "notification_topic": None,
            }

        endpoint = self._normalize_endpoint(
            info.get("endpoint") or info.get("iotEndpoint") or info.get("domain")
        )
        region = info.get("region")
        identity_id = info.get("identityId")
        client_id = info.get("clientId") or identity_id
        developer_token = info.get("token") or info.get("username")
        identity_pool_id = info.get("identityPoolId")
        credentials = info.get("credentials")
        if not isinstance(credentials, dict):
            credentials = None

        sts_credentials_usable = bool(
            credentials
            and all(
                credentials.get(key)
                for key in ("accessKeyId", "secretAccessKey", "sessionToken")
            )
        )
        mode: str | None = None
        if endpoint and region and sts_credentials_usable and client_id:
            mode = "sts"
        if (
            mode is None
            and endpoint
            and region
            and identity_id
            and developer_token
            and identity_pool_id
        ):
            mode = "cognito"

        if not endpoint or not region:
            _LOGGER.warning(
                "FurryTail AWS policy is missing endpoint or region; MQTT is disabled"
            )
            mode = None
        elif mode is None:
            _LOGGER.warning(
                "FurryTail AWS policy has no usable STS or Cognito credentials; "
                "MQTT is disabled"
            )

        return {
            "mode": mode,
            "endpoint": endpoint,
            "region": str(region) if region else None,
            "client_id": str(client_id) if client_id else None,
            "identity_id": str(identity_id) if identity_id else None,
            "token": str(developer_token) if developer_token else None,
            "identity_pool_id": str(identity_pool_id) if identity_pool_id else None,
            "credentials": credentials if sts_credentials_usable else None,
            "credentials_expiration": (
                credentials.get("expiration") if credentials else None
            ),
            "notification_topic": info.get("notificationTopic"),
        }

    async def async_exchange_cognito_credentials(
        self, policy: dict[str, Any]
    ) -> dict[str, Any]:
        """Exchange Granwin developer identity data for temporary AWS keys."""
        region = policy.get("region")
        identity_id = policy.get("identity_id")
        developer_token = policy.get("token")
        if not region or not identity_id or not developer_token:
            raise FurryTailApiError(
                "FurryTail Cognito policy is missing region, identityId, or token"
            )

        url = f"https://cognito-identity.{region}.amazonaws.com/"
        headers = {
            "X-Amz-Target": "AWSCognitoIdentityService.GetCredentialsForIdentity",
            "Content-Type": "application/x-amz-json-1.1",
        }
        body = {
            "IdentityId": identity_id,
            "Logins": {"granwin": developer_token},
        }
        response = await self._session.request(
            "POST", url, headers=headers, json=body
        )
        async with response:
            response.raise_for_status()
            payload = await self._response_payload(response)
        credentials = payload.get("Credentials") if isinstance(payload, dict) else None
        if not isinstance(credentials, dict) or not all(
            credentials.get(key)
            for key in ("AccessKeyId", "SecretKey", "SessionToken")
        ):
            raise FurryTailApiError(
                "Cognito response did not contain usable temporary credentials"
            )
        return {
            "access_key_id": credentials["AccessKeyId"],
            "secret_access_key": credentials["SecretKey"],
            "session_token": credentials["SessionToken"],
            "expiration": credentials.get("Expiration"),
        }
