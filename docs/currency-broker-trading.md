# Currency broker proposals and execution module

Status: development draft. Broker execution and proposal delivery default to
disabled. This branch does not change running service configuration, attach a
real account, submit an order, or install a protective order.

## Requested workflow

The Currency research policy prepares an immutable proposal for the exact
CNYRUBF instrument. A private message from @AxednewsI_bot carries signed approve
and reject buttons. A persisted, authorized approval can be claimed once. Before
submission the coordinator checks current broker facts and the same canonical
event again. Actual broker execution stages, rather than acknowledgement of an
order, change the independent Currency ledger.

The existing @axednewz / Axed News notification outbox remains a paper-event
notification stream. Its OPEN/ADD/REDUCE/CLOSE records are not instructions for
the broker. News ADMIN_USER_ID and news publication approval do not authorize
trading decisions.

## Modules

| Module | Responsibility |
|---|---|
| veritas_currency_trade_plan.py | Pure canonical event validation, exact contract sizing and immutable proposal terms |
| veritas_trade_approvals.py | PostgreSQL proposal, signed decision, delivery barrier and one-time submission barrier |
| veritas_tbank_trading.py | Scoped broker RPC transport, order identifiers, acknowledgements and actual executions |
| veritas_currency_trading.py | Revalidation, claim before broker I/O and reconciliation |
| veritas_currency_trade_ledger.py | Independent allocation, actual fills, commissions, funding records and held strategy terms |
| veritas_currency_trade_service.py | Explicit account binding, authoritative broker facts and protected internal HTTP operations |
| veritas_trade_telegram.py | Private Telegram delivery and forwarding of authenticated callbacks |
| bot.py | Existing single getUpdates consumer, with durable trade callback handling before update acknowledgement |

The service endpoint is /internal/currency-trading/. It uses a separate
X-Veritas-Trade-Key and bounded request body. The callback endpoint records an
owner decision; the polling coordinator performs any later approved execution.
No GET route enables trading.

## Canonical policy and contract arithmetic

CNYRUBF has an asset-specific entry cost multiple of 1.1. The effective minimum
expected move is max(0.0019, 1.1 * modeled round-trip execution cost). Commission
remains 0.0004 per side. Other instruments retain their canonical cost multiple.
The native structural event, current structural entry geometry, source identity,
local confirmation and final execution economics remain required.

Currency starts with a separate 10,000 RUB allocation. The existing canonical
maximum gross exposure is 10 times that allocation; its hard drawdown threshold
is 35%. The full broker account equity is not treated as Currency capital.

One lot's point-price notional is:

    limit_price * tick_value_rub / tick_size * lot_size

Desired whole lots are rounded down from the canonical admitted notional.
Existing lots are subtracted for ADD. The result is limited by actual available
margin, the broker's current maximum lots, commission reserve and the whole
position's structural stop risk cap.

A maximum leverage of 10 does not turn a canonical admitted fraction of 0.8
into 8.0. A 10,000 RUB allocation at a fraction below 1 may admit less than one
CNYRUBF contract. In that case the proposal is blocked. The module does not
silently round the position up, reinterpret the risk policy, or convert paper
units directly into contracts.

The exact broker instrument UID is c300543d-aa18-4249-b110-615409dde036.
Broker metadata supplies tick size, tick amount, lot and initial margin.
Instrument and quote identities must agree; the adapter separately allowlists
the exact UID.

OPEN and ADD preserve the canonical event, timeframe and structural geometry.
ADD uses the held position's approved stop, target and source. It cannot silently
replace those levels.

REDUCE and CLOSE use a separate reduction path. New-entry potential, entry
margin and drawdown admission cannot veto a verified reduction. Quantity may
not exceed either the actual broker holding or the managed Currency holding,
and the order must not reverse the position. Unresolved working orders still
require reconciliation to avoid a race.

## Approval identity and durability

A proposal binds all financial terms, canonical event, instrument, account,
policy revision, ledger revision, execution environment, owner, private chat,
bot, expiry and a unique client order UUID. JSON financial values use decimal
strings. Changes to signed terms require a different valid proposal; they
cannot be applied under an old approval.

Only the explicitly configured Telegram user, in that user's private chat,
on the original message sent by the configured bot, can approve. A channel
subscriber, news administrator, forwarded message or copied callback cannot
inherit this permission.

Delivery commits DELIVERY_SENDING before sendMessage. If the Telegram response
is lost, the proposal becomes DELIVERY_UNKNOWN and is not blindly sent again.
A repeat acknowledgement of the same original message is safe.

A signed durable decision must exist before claim_approved can commit SENDING.
A repeated button, callback replay, concurrent worker or process restart cannot
create a second claim. An ambiguous broker response remains UNKNOWN and locks
the account/instrument scope pending broker reconciliation. It is never
interpreted as rejection and automatically retried with a new UUID.

The bot advances getUpdates offset only after the decision handler returns.
Temporary service or database failure leaves that update available for replay;
a definitive invalid callback can be acknowledged and discarded.

## Orders and accounting

Prepared orders are LIMIT with PRICE_TYPE_POINT and
TIME_IN_FORCE_FILL_AND_KILL. The broker may execute a smaller quantity and
cancel the remainder. FAK is intentionally represented as a possible partial
fill, not as a guarantee of a full fill.

Acknowledged does not mean filled. GetOrderState execution stages supply unique
trade IDs, quantities and POINT prices. Each execution is persisted once.
Cumulative commission observations are applied as deltas. Repeating unchanged
commission data cannot change the ledger revision or invalidate an approval.

Ledger NAV equals allocation plus realized P&L, less actual costs and funding,
plus marked unrealized P&L. Variation margin must not be added again on top of
the same economic P&L. Broker positions are reconciled against the independent
ledger; manual or unrelated trades in the same instrument cannot be adopted
silently.

Strategy stop and target levels are references for a separate closing proposal.
Approving an OPEN does not install a broker stop order. A later close requires
its own confirmation and can be delayed by the owner, the market, stale data,
an unavailable broker or an unresolved working order. The drawdown threshold
therefore does not guarantee a maximum realized loss.

The ledger supports explicit funding charge/credit records. Automated completeness
of perpetual-contract funding is a separate data requirement; fill commissions
alone do not prove that every funding charge has been reconciled. This draft
must not declare a live NAV complete across unverified funding periods.

## Configuration contract

No credentials or real account identifiers are stored in this branch.

| Configuration | Meaning |
|---|---|
| VERITAS_CURRENCY_TRADE_PROPOSALS_ENABLED | Enables construction of the isolated proposal service/worker; default off |
| TBANK_ACCOUNT_ID | Explicit account; never selected as the first account in a list |
| VERITAS_CURRENCY_TRADE_OWNER_USER_ID | Explicit numeric Telegram approving user |
| VERITAS_CURRENCY_TRADE_OWNER_CHAT_ID | Explicit private chat, required to equal the approving user ID |
| VERITAS_CURRENCY_TRADE_BOT_ID | Explicit numeric ID of @AxednewsI_bot |
| VERITAS_CURRENCY_TRADE_SERVICE_KEY | Separate internal service authentication secret, at least 32 bytes |
| VERITAS_CURRENCY_TRADE_APPROVAL_KEY | Separate stable signing secret, at least 32 bytes and different from the service key |
| VERITAS_CURRENCY_TRADE_SERVICE_URL | Pinned HTTPS service origin used by the Telegram bridge |
| VERITAS_CURRENCY_TRADE_ENVIRONMENT | production or sandbox; bound into every proposal |
| TBANK_API_TOKEN | Existing production broker token, read only from the service environment |
| TBANK_SANDBOX_TOKEN | Separate sandbox token |
| VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED | Currency execution gate; default off |
| VERITAS_LIVE_EXECUTION_ENABLED | Independent global execution gate; default off |
| VERITAS_LIVE_EXECUTION_ARMED | Independent arming gate; default off |
| VERITAS_CURRENCY_TRADE_MARGIN_ALLOWED | Explicit margin permissions; default off |

An existing market-data connection or a configured token does not establish that
the selected account is open and FULL_ACCESS. Actual binding verifies the exact
account and a flat position without active CNY orders, and allocates only the
independent Currency capital. This development task does not perform that
private-account binding or change token permissions.

Configuration is an operator handoff, not an activation performed by this branch.
Sandbox and production must never share an account-ledger namespace.

## Validation

The draft CI uses fake broker and Telegram transports and isolated PostgreSQL.
It does not receive production broker, Telegram or database credentials.
PostgreSQL trading tests require the explicit database name
ci_ephemeral_test_only and use temporary test schemas. Existing strategy-quality
tests retain their separate veritas_quality_test database.

The required tests cover exact lot rounding, native causal structural events,
stale market data, adverse price change, owner and environment changes, signed
terms, concurrent claims, transaction rollback, ambiguous submission, repeated
callbacks, partial fills, execution identity and ledger reconciliation.

A workspace failure required source recovery into checkpoint branches.
Pre-failure local test results are not a substitute for CI on the final commit.
See the draft pull request checks for the current validation result.

## Primary protocol references

- https://developer.tbank.ru/invest/services/orders/methods
- https://developer.tbank.ru/invest/intro/intro/token
- https://developer.tbank.ru/invest/services/accounts/users
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/operations.proto
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/instruments.proto
