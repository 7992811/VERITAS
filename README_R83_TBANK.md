# R83 — T-Invest read-only connection

The Python service can authenticate to T-Invest through gRPC, read accounts and
an explicitly selected portfolio, resolve exact instrument identifiers, download
completed 5-minute/hourly candles, load order books and receive streamed prices.
This is a data connection, not a live broker execution adapter.

## Activation

Create a production **read-only** token in the T-Invest account and add it to the
existing Render service's Environment as `TBANK_API_TOKEN`. Save and redeploy.
Token issuance and bank login remain the account owner's action. Never put a
token into Git, the public dashboard, query strings or log output.

Status page: `/integrations/tbank`.
Status JSON: `/api/v1/integrations/tbank`.

No token: `WAITING_TOKEN`, zero background threads and no network requests.
Token present: the background worker authenticates independently of app startup.
The page and status response distinguish connection, authentication errors,
missing instruments and stale heartbeat. An authenticated account with no matching
instrument is not advertised as a working quote source for that instrument.

Optional environment variables:

- `TBANK_TICKERS`: JSON map of VERITAS label to exact broker ticker; at most seven.
  Default `{"CNYRUBF":"CNYRUBF"}`. An absent or ambiguous match is reported;
  another instrument/expiry is never substituted. For NQ, GOLD, BRENT, MOEX,
  BTC and ETH the correct contracts need separate catalog verification.
- `TBANK_ACCOUNT_ID`: account returned by the token. If there is exactly one
  account, it is selected automatically for read-only portfolio loading. Multiple
  accounts require explicit selection. No real account is linked to paper ledgers.
- `VERITAS_APP_AUTH_TOKEN`: existing app authentication secret. If unset, the
  existing `VERITAS_AUTOMATION_TOKEN` is accepted for private read routes.
  If neither exists, those routes are denied. Broker tokens are never app passwords.

## Data access

Public, cached-only routes (no user-triggered broker requests):

- `/api/v1/integrations/tbank/market-data`
- `/api/v1/integrations/tbank/candles?asset=CNYRUBF&interval=1h` (or `5m`)

Protected routes require `X-Veritas-Token`, checked in constant time:

- `/api/v1/integrations/tbank/accounts`
- `/api/v1/integrations/tbank/portfolio`

Quotes keep the broker's observation timestamp, separate receipt timestamp and
source. Prices over 120 seconds old, missing timestamps and future timestamps are
not classified as fresh. Older stream messages cannot replace newer observations.
Prices are explicitly broker-native, not normalized into existing paper-instrument
prices or multiplied by lot size. Only complete historical candles are exposed.
The history cache contains a rolling day of 5m and seven days of 1h data, up to
2,000 bars per instrument/interval. This adapter does not activate trading signals
from that limited history or mix it with another provider's history.

Accounts, order books and history refresh every 60 seconds. A single bounded
stream supplies last prices. Unary RPC deadline: eight seconds. Stream deadline:
300 seconds followed by resubscription. Failed refreshes back off to 300 seconds.
Cache reads never block on broker network I/O. Full RPC exception details and
authorization metadata are not put into JSON responses or application logs.

## Transport provenance and limits

The current bank-hosted SDK registry returned HTTP 502 in the development
workspace. The PyPI package with the same name is quarantined and is not used.
Instead, the adapter uses standard `grpcio` and `protobuf` with a namespaced
read-only subset of the bank's published wire contracts. Source commit and the
scoped CA certificate provenance are in `vendor/tbank/NOTICE.md`.

TLS certificate and hostname verification stay enabled. The bank's documented
Russian Trusted Root CA is added only to this channel's system trust roots.
Target is fixed to `invest-public-api.tbank.ru:443`; credentials cannot be
redirected with a custom-host environment variable.

The transport has an explicit read-only method allowlist. No order, cancellation,
withdrawal or transfer request can be sent. No changes to existing signals,
paper portfolios, source bindings, fees or funding are made by this release.

## Validation

`python -m unittest -v test_veritas_tbank.py` checks actual local gRPC unary/stream
serialization and authentication metadata, RPC error sanitization, blocked write
methods, exact ticker/UID matching, private endpoint authorization, cache privacy,
timestamp freshness, complete candles and zero-token startup.

The R82 cost/profit-protection/move-floor regressions are also run. Dependencies
have binary distributions for the service's Python 3.14 runtime.

Local public-endpoint TLS probing timed out through the workspace HTTP proxy.
Authenticated access, live availability of CNYRUBF and data latency must be
verified on Render after the owner supplies the token. A deployed `WAITING_TOKEN`
state is not evidence of an authenticated broker connection.
