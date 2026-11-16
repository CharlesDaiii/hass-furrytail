# FurryTail Home Assistant integration

Version 0.3.6 of this custom integration connects a FurryTail Automatic Cat
Litter Box on the current Granwin cloud platform to Home Assistant. It uses periodic HTTPS
snapshots for reliable state and AWS IoT MQTT push updates when the cloud
returns usable temporary credentials. Device control (buttons and switches)
works through the app's own HTTPS fallback endpoint — manual clean was
verified end-to-end against a real device on 2026-11-15.

## Installation via HACS

Add this repository as a custom repository in HACS (category: Integration),
install "FurryTail", and restart Home Assistant. Or copy
`custom_components/furrytail` into your configuration directory manually.

## Requirements

- Home Assistant with config-flow custom integration support.
- A FurryTail app access token for an already paired litter box.
- Internet access from Home Assistant. No local device API is known.

Home Assistant installs the declared `paho-mqtt>=2.0.0` dependency. The
Home Assistant MQTT integration is not required, and no AWS SDK is installed.

## Installation and setup

1. Copy `custom_components/furrytail` to
   `<config>/custom_components/furrytail` in the Home Assistant configuration
   directory.
2. Restart Home Assistant.
3. Open **Settings → Devices & services → Add integration** and select
   **FurryTail**.
4. In the FurryTail app's developer tools, inspect any request and copy the
   raw `Authorization` header value into the Access token field. App tokens
   are valid for 30 days.
5. Leave MAC empty for the first discovered device. If the account has several
   devices, enter the desired address in a placeholder-shaped format such as
   `AA:BB:CC:DD:EE:FF` to select it.
6. Device discovery needs your home's Place ID (shown in the app developer
   tools with your home info). You can enter it during setup or later under
   the integration's **Configure** form. Without it, state polling still
   works but discovery — and the device ID it provides — does not.

There is no account-ID field. The MQTT prefix is the discovered 16-character
`productKey`, not an account identifier. Existing version 0.1.0 entries still
read their old `account_id` value as a migration fallback when no discovered
`product_key` is stored.

## Entities

The integration defines these entities for each litter box:

- Sensors: Cat weight (kg), Poop count, Stay interval, Device status, Task
  type, Device fault, Do not disturb duration, and Indicator light duration.
- Binary sensors: Cat present, Online, and Fault.
- Switches: Auto clean, Auto clean timer, Do not disturb, Indicator light,
  Clean shake, and Child lock.
- Buttons: Manual clean, Flatten, Sand change, Reset poop storage, and Roller
  reset.

Missing cloud attributes remain unknown. Unknown numeric status values are
preserved rather than rejected.

## Protocol summary

The integration validates the app token with
`POST /device/query/device/property`. Every cloud request sends the token as
the raw `Authorization` header value. JWT claims supply the 30-day expiry;
refresh tries `/device/user/refresh` and `/auth/refresh` proactively near
expiry and once reactively after HTTP 401 or business code 10004/20013.

Device discovery (`POST /device/user/device/list`) obtains the MAC, device
ID, product ID, `productKey`, and thing name. The Granwin backend validates
request context in sequence: the body must carry the app-baked `merchantId`
(`100000000000000000`, the 吾尾物联网平台 merchant constant — the same for
every FurryTail user, not a credential) and the account's `placeId`, or it
answers "Merchant ID cannot be empty!" / "Place ID cannot be empty!".
Attribute reads first use the app-observed endpoint:

```text
POST /device/query/device/property
{"mac":"AABBCCDDEEFF"}
```

This proven MAC route is the only attribute-read path. Values such as `"0"`,
`"1"`, `"true"`, and empty strings are decoded to integers, booleans, and
unknown values. Attribute 21 is reported in grams and exposed as kilograms.

The SDK-form MQTT topics include their leading slash:

```text
/<productKey>/<thingName>/user/update
/<productKey>/<thingName>/user/get
```

The integration accepts both observed `value` and SDK `data` push frames. When
both contain an attribute, `data` wins. Control uses the observed data type 81.

```json
{
  "data": {
    "3": {
      "type": 81,
      "value": 1
    }
  },
  "time": 1700000000
}
```

The integration tries `/device/add/policy` (the path the current app uses),
then `/device/user/client/generate` and `/device/client/generate`, for either
direct STS temporary credentials or Cognito developer identity fields. If no
route works, it remains safely in polling-only mode. Cognito mode exchanges returned fields with the regional
Cognito Identity JSON API using provider `granwin`. Both modes connect to AWS
IoT over WebSocket/SigV4 on port 443 with paho-mqtt. The presigned `/mqtt` path
is built with Python's standard `hmac`, `hashlib`, and `urllib.parse`; no
`awsiotsdk`, `awscrt`, or `boto3` dependency is used.

MQTT control publishes QoS 1 and non-retained. If MQTT is unavailable, control
falls back to the app's HTTPS control endpoint:

```text
POST /device/control/device
{"mac": "AABBCCDDEEFF", "propertyMap": {"3": 1}}
```

Note the plain property map — the typed `{"type", "value"}` envelope above is
the MQTT payload shape only. The similar-looking `POST /device/control`
endpoint authenticates the same token but always answers "user has no
permission for this operation"; it is not the control route. Manual clean
(attribute 3, value 1) was verified live through this HTTPS fallback on
2026-11-15: the box ran a full clean cycle.
HTTP polling remains the state source of truth while MQTT updates are merged
between polls. Direct STS Policy credentials do not auto-renew; MQTT setup
requests a fresh Policy near expiration and after a disconnect retry.

## Known limitations

- Tokens currently must be copied from the FurryTail app again after they
  expire if both known refresh routes reject them.
- Manual clean (attribute 3, value 1) is verified live over both MQTT
  (app trace) and the HTTPS fallback. Value 1 for Flatten, Sand change,
  Reset poop storage, and Roller reset is presumed by analogy.
- This remains a cloud integration; vendor availability and temporary AWS
  credentials affect live updates and control.

## Credits

Protocol findings were cross-checked against the public MITM notes in
[CamiloValderruten/hass-furrytail](https://github.com/CamiloValderruten/hass-furrytail),
which first documented the `/device/control/device` HTTPS fallback — thank
you. The Granwin SDK analysis and the merchant/place context behaviour were
worked out independently against a live device.

## Development and offline validation

Run the checks without contacting vendor services or opening MQTT sockets:

```bash
python3 -m py_compile custom_components/furrytail/*.py test_offline.py
python3 test_offline.py
python3 -c 'import json; json.load(open("custom_components/furrytail/manifest.json"))'
```

`test_offline.py` uses only canned responses and in-memory fake sessions. It
does not import the Home Assistant-dependent coordinator, config flow, or
integration entrypoint.
