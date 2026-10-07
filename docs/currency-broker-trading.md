# Currency broker proposals and execution module

The operator console is `/integrations/trading`. Its static login page is public;
account, position, order and fill data require a scoped private session. The
execution service remains default-off in code, and the console starts paused
with execution permission off. Deployment alone does not select an account,
bind an approving owner, submit an order, or install a protective broker order.

## Private setup and everyday access

1. Open the expiring, single-use setup invitation. Its secret is carried in the
   URL fragment, removed immediately, and exchanged for a 12-hour HttpOnly,
   Secure, SameSite=Strict cookie. Neither broker tokens nor permanent service
   or signing keys are sent to the browser.
2. Select one exact open FULL_ACCESS broker account. The service never picks
   the first account automatically and cannot replace an existing binding.
3. Create the Telegram pairing link, open the private chat with @AxednewsI_bot,
   then return to the same authenticated console and confirm the observed user
   identity. The bot's getMe ID/username and the sender's private identity must
   match; forwarded messages and group messages do not establish ownership.
4. Complete the 10,000 RUB allocation binding against actual broker facts. The
   selected CNY instrument must be flat and free of working orders. If the
   initial bind fails, the explicit **reconcile** action retries the same saved
   account/owner binding after the condition is resolved.
5. Resume proposals and, separately, allow execution of confirmed proposals.
   Every opening, addition, reduction and closing still requires its own signed,
   unexpired Telegram confirmation and final validation.

For later login the bound owner sends `/veritas` to the same private bot. The
bot returns a one-use 10-minute login link scoped to the saved environment and
owner/bot tuple. A new request revokes older unused login links. Session renewal
does not change trading permissions. Every browser mutation requires exact
same-origin and session CSRF proof; there is no browser approve/execute endpoint.

The dashboard reads current broker facts and the durable ledger without applying
fills, reconciliation freezes or high-water updates. Actual reconciliation runs
on explicit POST or the existing worker. Missing observations remain unknown,
instead of displaying empty positions or zero costs as confirmed facts.

## Requested workflow

The Currency research policy prepares an immutable proposal for the exact
CNYRUBF instrument. A private message from @AxednewsI_bot carries signed approve
and reject buttons. A persisted, authorized approval can be claimed once. Before
submission the coordinator checks current broker facts and the same canonical
event again. Production OPEN/ADD also require a separate, explicitly affirmative
whole-account live admission verdict. Actual broker execution stages, rather than acknowledgement of an
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
| veritas_currency_trade_console.py | Private sessions, exact account selection, two-channel owner pairing and serialized execution permission |
| veritas_currency_trade_ui.py | Mobile Russian console with observed positions, proposals, fills and admission blockers |
| veritas_currency_console_telegram.py | Private owner pairing and renewable owner login through the existing bot consumer |
| veritas_currency_live_admission.py | Unchanged LIVE authority fed by exact whole-account broker facts and audited evidence |
| veritas_currency_settlement.py | Actual funding observations, corrections and a numerically checked broker settlement bridge |
| veritas_trade_telegram.py | Private Telegram delivery and forwarding of authenticated callbacks |
| bot.py | Existing single getUpdates consumer, with durable trade callback handling before update acknowledgement |

The service endpoint is /internal/currency-trading/. It uses a separate
X-Veritas-Trade-Key and bounded request body. The callback endpoint records an
owner decision; the polling coordinator performs any later approved execution.
No GET route enables trading.

## Canonical policy and contract arithmetic

CNYRUBF uses the current canonical entry cost multiple of 1.1. The effective minimum
expected move is max(0.0019, 1.1 * modeled round-trip execution cost). Commission
remains 0.0004 per side. Other instruments retain their canonical cost multiple.
The native structural or confirmed DAILY_MA_REBOUND event, current entry geometry,
source identity, local confirmation and final execution economics remain required.
The daily-average policy and strategy epoch from the current canonical runtime
are preserved.

Currency starts with a separate 10,000 RUB allocation. The existing canonical
maximum gross exposure is 10 times that allocation; its hard drawdown threshold
is 35%. The full broker account equity is not treated as Currency capital.

One lot's point-price notional is:

    limit_price * tick_value_rub / tick_size * lot_size

Desired whole lots are rounded down from the canonical admitted notional.
Existing lots are subtracted for ADD. The result is limited by actual available
margin, the broker's current maximum lots, commission reserve and the whole
position's structural stop risk cap. Available margin is bounded by both
GetPositions money minus blocked cash and GetWithdrawLimits money minus blocked
cash and blockedGuarantee. Missing withdrawal-limit evidence gives zero capacity
for a new entry or initial allocation binding; it does not veto a valid close.

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
and the approved quantity must not reverse the position at that snapshot.
Unresolved working orders still require reconciliation to avoid a race. This
local reduce-only check is not a broker-enforced atomic position condition:
manual or external CNY orders racing between the final read and submission can
change the holding. Exclusive management of this instrument is an operating
assumption; a detected mismatch freezes new risk.

## Existing live authority remains mandatory

Currency allocation limits and the whole-account live risk policy use different
capital scopes. The Currency 10x / 35% policy and its 2% structural idea-risk cap
do not replace CTC.LIVE_RISK_POLICY or the existing production admission checks.

A production OPEN or ADD must obtain an affirmative verdict from the injected
live_admission authority after fresh Currency validation and before the durable
submission claim. The callback receives a copy of the exact approved terms,
fresh facts and the current time. It must return a mapping with eligible exactly
True and an explicit empty blockers list/tuple. Missing authority, an exception,
malformed output or a negative verdict blocks with LIVE_ACCOUNT_ADMISSION_REQUIRED.
The coordinator checks Currency freshness again after this callback.

The factory now supplies the concrete `WholeAccountLiveAdmission` authority.
It reads actual total broker account equity, holdings, working orders and stops,
then invokes the existing LIVE promotion, calibration, risk and kill gates. A
production preflight happens before a proposal is delivered, and the same gates
are evaluated again before submission. Missing verified model validation or
cash-flow-adjusted account history remains an explicit blocker; a legacy paper
backtest, configured token, global flag or owner approval cannot synthesize it.
See [currency-live-authority.md](currency-live-authority.md) for the audited intake
format, inspected current research producers, exact support and dependencies.
The dashboard displays cached admission diagnostics separately from execution
permission; a cached pass is never submission authority.
Sandbox transport and verified production CLOSE/REDUCE do not invoke this
new-risk admission callback. Existing policy limits are not changed.

## Approval identity and durability

A proposal binds all financial terms, canonical event, instrument, account,
policy revision, ledger revision, execution environment, owner, private chat,
bot, expiry and a unique client order UUID. JSON financial values use decimal
strings. Native signal evidence is also retained as a signed JSON string, preserving
the numeric timestamps and proof types required by the canonical MA validator.
Before revalidation its normalized view must exactly match entry_context; the
validator consumes the original native evidence. Changes to signed terms require
a different valid proposal; they cannot be applied under an old approval.

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

Console pause/disable and the final send boundary share a PostgreSQL lock on
the exact environment configuration row. A successful pause cannot race a
later submission using a stale cached permission. An already transmitted order
may still execute; pausing is not cancellation. Final quote and evidence
lifetimes are checked after claim and immediately before broker mutation,
including after slow adapter preflight reads.

Protective exits have durable proposal generations. A fresh price and deadline
require a newly signed confirmation after an expired or blocked pre-submission
proposal, or after a fully reconciled zero-fill cancellation/rejection. An
unresolved submission or unaccounted execution cannot be replaced by a new
generation. Owner rejection is not automatically re-proposed. Opposite-signal
exits use the held timeframe and pinned source; an entry economics filter cannot
veto this reduction path.

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

The settlement reconciler automatically ingests actual FUNDING=70 charges,
credits and cumulative corrections. It retains immutable observed evidence and
can apply authoritative downward fee corrections. The ledger never adds cash
variation margin twice on top of price P&L. Completing a cursor does not prove
final settlement: GetBrokerReport supplies trade commissions but not a complete
perpetual funding/variation-margin cash section. The trusted statement-provider
interface numerically checks that bridge against exact executions and cash
operations. Until a real final statement adapter supplies that evidence, later
OPEN/ADD remains blocked with the precise missing-settlement reason. CLOSE and
REDUCE remain available when their own broker/position conditions are valid.
Missing settlement data cannot advance the high-water mark or be replaced with
an elapsed-hour heuristic, environment override or fabricated report.

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
| VERITAS_CURRENCY_TRADE_CONSOLE_ENABLED | Enables authenticated operator setup and persisted identity/permission |
| VERITAS_CURRENCY_TRADE_SETUP_HASH | SHA-256 of the single-use initial invitation; raw code is never persisted |
| VERITAS_CURRENCY_TRADE_SETUP_EXPIRES_AT | Aware ISO expiry of the initial invitation |
| VERITAS_CURRENCY_TRADE_SESSION_KEY | Separate stable CSRF signing secret, at least 32 bytes |

An existing market-data connection or a configured token does not establish that
the selected account is open and FULL_ACCESS. Actual binding verifies the exact
account and a flat position without active CNY orders, and allocates only the
independent Currency capital. This development task does not perform that
private-account binding or change broker token permissions automatically.

The console persists explicit account/owner selection; configured environment
identities, when present, must match it. Sandbox and production never share an
account-ledger namespace or a valid browser session.

## Validation

CI uses fake broker and Telegram transports and isolated PostgreSQL.
It does not receive production broker, Telegram or database credentials.
PostgreSQL trading tests require the explicit database name
ci_ephemeral_test_only and use temporary test schemas. Existing strategy-quality
tests retain their separate veritas_quality_test database.

The required tests cover exact lot rounding, native causal structural events and
daily MA proof serialization, stale market data, adverse price change, owner and
environment changes, signed terms, concurrent claims, transaction rollback,
ambiguous submission, repeated callbacks, exit generations, partial fills,
execution identity, margin facts and ledger reconciliation. The HTTP and Telegram
integration uses the actual signed proposal repository with fake network transports.

See the pull request checks for the validation result on its current commit.
The repository also retains a separate nonblocking legacy execution audit; its
status must not be presented as part of an unqualified all-tests-passed claim.

## Primary protocol references

- https://www.moex.com/a8141
- https://developer.tbank.ru/invest/services/operations/methods
- https://developer.tbank.ru/invest/services/orders/methods
- https://developer.tbank.ru/invest/intro/intro/token
- https://developer.tbank.ru/invest/services/accounts/users
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/operations.proto
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/instruments.proto
