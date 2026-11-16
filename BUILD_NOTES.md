# Build notes

Version 0.2.0 was completed on 2026-11-13 without logging in, contacting a
vendor or AWS endpoint, or sending a command to a device.

## What changed in 0.2.0

- `const.py`: replaced the incorrect account-ID topic prefix with discovered
  `productKey`, device ID, and thing-name fields; added leading-slash topic
  builders, SDK token-refresh leeway, and per-command data types with 81 as a
  compatibility fallback.
- `api.py`: implemented login, prefixed access-token authentication, proactive and reactive
  token refresh, device/product discovery, paginated data-point type lookup,
  the observed attribute-read path with the SDK device-ID fallback, HTTPS
  control, AWS Policy normalization, and the unsigned Cognito developer-auth
  exchange.
- `mqtt_client.py`: added pure-Python AWS SigV4 presigning for `GET /mqtt` and
  paho-mqtt WebSocket transport on port 443. This uses only `hashlib`, `hmac`,
  and `urllib.parse`; it deliberately does not add `awsiotsdk`, `awscrt`, or
  `boto3`. Subscribe is QoS 0, control publish is QoS 1/non-retained, and both
  `value` and `data` push frames are parsed.
- `coordinator.py`: keeps periodic HTTP snapshots as the source of truth while
  merging MQTT pushes, including the SDK rule that HTTP remains preferred just
  after MQTT connects. It supports direct STS Policy credentials and Cognito
  exchange credentials, resolves data-point types before control, and falls
  back to HTTPS when MQTT is unavailable. The periodic poll now triggers a
  non-blocking MQTT re-setup after a connection drop or when temporary
  credentials near expiry, with a 60-second retry cooldown and no overlapping
  setup tasks. Retries only occur after an MQTT client previously existed. If
  incomplete Policy data means no client was ever created, the never-connected
  entry remains polling-only until it is reloaded.
- `config_flow.py` and `__init__.py`: setup and reauthentication now log in and
  discover device metadata. MAC is optional and can select one device from an
  account with several. Version 0.1.0 `account_id` data is read only as a
  legacy `productKey` fallback, and newly discovered fields are persisted.
- `manifest.json`, strings, and English translation: bumped the integration to
  0.2.0 and removed the account-ID field and blocked-setup wording.
- `test_offline.py`: expanded the fake session to record requests and serve
  sequential canned responses without opening sockets.

The paho/SigV4 approach follows the analyzed SDK transport while keeping the
existing dependency set. The coordinator stores temporary AWS credential
expiration and replaces the MQTT client with a freshly authorized one when the
periodic poll sees a disconnect or credentials nearing expiry. HTTP reads first
use the app-observed MAC endpoint and then the SDK device-ID endpoint. MQTT
parsing intentionally accepts both observed `value` and SDK `data` keys, with
`data` winning when both contain the same attribute.

## Offline verification

- `python3 -m py_compile custom_components/furrytail/*.py test_offline.py`
  passed.
- `python3 test_offline.py` printed `offline tests passed`. Coverage includes
  login payload variants, prefixed access and refresh
  headers, one refresh-and-retry cycle, data-point lookup and fallback type 81,
  leading-slash topics, `value`/`data` push frames, deterministic and
  key-sensitive SigV4 output, Cognito request/response shape, the read-endpoint
  fallback, and STS/Cognito/incomplete Policy normalization.
- `manifest.json`, `strings.json`, and `translations/en.json` parsed as valid
  JSON.
- The requested scans found no prohibited device address, private LAN address,
  AWS access-key prefix, or non-placeholder credential in the integration,
  test, README, or this file.
- Home Assistant is not installed in this offline environment, so an actual HA
  runtime setup/import and a live cloud/MQTT connection were not verified.

## Version 0.3.0

- Replaced the retired password-login model with a user-supplied 30-day app
  token, raw `Authorization` headers, JWT expiry parsing, token validation, and
  Home Assistant reauthentication when the cloud rejects a token.
- Removed retired cloud routes and added ordered fallbacks for refresh, device
  discovery, and AWS policy generation. Unverified discovery and AWS failures
  now degrade to stored metadata or polling-only operation.
- Kept HTTP property polling as the source of truth, retained MQTT/SigV4 and
  all 22 entities unchanged, fixed the HTTPS control route, and standardized
  control data type 81.
- Reworked the offline suite around programmatically built dummy JWTs, string
  and integer business codes, raw auth headers, fallback order, and graceful
  failure behavior without network access or real credentials.
