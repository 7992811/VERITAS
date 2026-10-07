# Currency broker proposals and execution module

Status: development draft. Broker execution and proposal delivery default to
disabled. This branch does not change running service configuration, attach a
real account, submit an order, or install a protective order.

## Requested workflow

The Currency research policy prepares an immutable proposal for the exact
CNYRUBF instrument. A private message from @AxednewsI_bot carries signed approve
and reject buttons. A persisted, authorized approval can be claimed once. Before
submission the coordinator checks current broker facts and the same canonical
event again. Production OPEN/ADD also require a separate, explicitly affirmative
whole-account live admission verdict. Actual broker execution stages, rather than acknowledgement of an
order, change the independent Currency ledger.

The existing paper-event notification stream retains its explicit paper labels.
A separate, default-off broker outbox mirrors verified actual fills to
@axednewz (numeric channel -1002967459105), with production/sandbox labels.
Neither channel stream authorizes a broker order. News ADMIN_USER_ID and news
publication approval do not authorize trading decisions.

## Modules

| Module | Responsibility |
|---|---|
| veritas_currency_trade_plan.py | Pure canonical event validation, exact contract sizing and immutable proposal terms |
| veritas_trade_approvals.py | PostgreSQL proposal, signed decision, delivery barrier and one-time submission barrier |
| veritas_tbank_trading.py | Scoped broker RPC transport, order identifiers, acknowledgements and actual executions |
| veritas_currency_trading.py | Revalidation, claim before broker I/O and reconciliation |
| veritas_currency_trade_ledger.py | Independent allocation, actual fills, commissions, funding records and held strategy terms |
| veritas_currency_trade_funding.py | Bounded operation snapshots, signed statement review, correction deltas and settlement freshness |
| veritas_currency_live_admission.py | Signed evidence import, entire-account reconciliation and the existing production authority |
| veritas_currency_trade_service.py | Explicit account binding, authoritative broker facts and protected internal HTTP operations |
| veritas_trade_telegram.py | Private Telegram delivery and forwarding of authenticated callbacks |
| veritas_currency_broker_notifications.py | Transactional, scoped outbox of verified broker execution facts |
| veritas_currency_broker_delivery.py | Default-off channel delivery with pinned bot/channel identity and ambiguous-send barriers |
| bot.py | Existing single getUpdates consumer, with durable trade callback handling before update acknowledgement |

The service endpoint is /internal/currency-trading/. It uses a separate
X-Veritas-Trade-Key and bounded request body. The callback endpoint records an
owner decision; the polling coordinator performs any later approved execution.
No GET route enables trading. Authenticated status reads cached metadata only;
it does not initialize storage, read the broker, poll proposals or execute an
approved proposal.

## Canonical policy and contract arithmetic

CNYRUBF has an asset-specific entry cost multiple of 1.1. The effective minimum
expected move is max(0.0019, 1.1 * modeled round-trip execution cost). Commission
remains 0.0004 per side. Other instruments retain their canonical cost multiple.
The native structural or confirmed DAILY_MA_REBOUND event, current entry geometry,
source identity, local confirmation and final execution economics remain required.
The current CAUSAL_QUOTE_STRUCTURE_V1 event uses the canonical timeframe-policy
dispatcher. Its broker quote is rebound only from the verified T-Invest UID and
actual order-book timestamp. An equal timestamp with a changed reference price
is rejected; the clock is never advanced to make the event appear fresh.
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

Broker economics explicitly use LIVE single-target evaluation, including the
existing hard reward/risk floor. The PAPER weighted target ladder and its
diagnostic-only reward/risk treatment do not authorize broker entries. Native
event proof is checked separately before the exact limit, rounded reference
stop/target and current spread enter this LIVE calculation. The same function,
holding duration and decision clock are used when revalidating the approval.
The terms state LIVE and SINGLE_TARGET_SEPARATE_CONFIRMATION explicitly; old
approvals without that execution policy require a new proposal.

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

The service factory supplies CurrencyLiveAdmission. It reads the entire
GetPortfolio/GetPositions/GetOrders account, uses the authoritative RUB account
valuation, and calls the unchanged veritas_live.authorize_candidate authority.
The Currency 10,000 RUB ledger is never substituted for whole-account NAV.
Held CNY and the proposed addition are counted once each, and the combined asset
limit is checked before admission. Full CNY stop risk includes modeled execution
costs; those costs also enter the aggregate and correlated-risk checks.

Two independently signed evidence records are required for the exact approved
terms: ACCOUNT_CONTROLS and MODEL_ADMISSION. Both bind account, instrument,
environment, timeframe, source, model, policy and entry-code identity. Controls
bind the actual entire-account snapshot, high-water NAV, complete daily/weekly
UTC windows, position stops, correlations and kill-switch state. Model evidence
contains calibrated probability and the existing promotion metrics, with
separate references to OOS, vault, calibration, high-cost, shadow, CI and data
parity artifacts. Research confidence or a raw eligible/PASS flag is not accepted.

The issuer is a separate trusted evaluation process. The import endpoint
verifies its signature, validates every field, and appends an immutable record;
readback checks the signature again. It never generates successful evidence.
Controls expire within 30 seconds and model evidence within 900 seconds. A
newer invalid/expired record cannot fall back to an older positive record.
Missing issuer configuration or actual evidence remains a specific blocker;
connecting the provider is not itself proof of trading readiness.

The first version supports the exact managed CNY future and reconciled RUB
shares/ETFs. Other futures, bonds, options, foreign cash, virtual positions,
blocked balances, working orders, unknown stops and incomplete correlations
block new risk until their valuation/reconciliation is supported. They are not
silently omitted from the portfolio. Existing price-to-stop risk semantics
remain in force for supported non-CNY positions.

No environment variable skips this authority. Authenticated status reports
provider configuration, cached evidence diagnostics and the new-risk blocker
separately from the global execution flag.
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

News collection runs in one bounded worker without a backlog. The main bot
loop remains the sole getUpdates consumer, so a slow news/LLM request cannot
hold that loop before it reads a trade confirmation. News pause and disabled
startup notification behavior are preserved.

### Private owner commands

- `/currency_status` reports configured environment, masked account, cached
  binding state, last completed poll and its blocker, pending approvals and
  unsettled executions. Missing/old observations are explicitly marked unknown
  or stale. The command never calls poll.
- `/currency_bind` describes the selected account and issues a one-time code
  valid for 120 seconds. Repeating the command with that code rechecks the full
  account/instrument/environment scope before requesting ledger binding. A
  changed scope, restart, invalid code or expiry requires a fresh challenge.
  Binding does not transfer cash, change execution flags or create orders.

Only the configured numeric trading owner in that owner's private chat can use
these commands. A news administrator does not acquire trading authority.

### Actual broker execution notices in @axednewz

`VERITAS_CURRENCY_BROKER_NOTIFICATIONS_ENABLED` independently enables an
informational channel mirror. Each newly recorded execution stage enqueues its
notice in the same PostgreSQL transaction as the fill and its cumulative fee
observation. Account, instrument, environment and broker trade ID deduplicate
the event. An ACK with no fills never creates a notice. The order-level fee is
not copied into every partial fill; the message states when a per-fill allocation
is unavailable. Position changes use broker execution chronology. An anomalous
or incomplete transition is labeled as requiring reconciliation and cannot
discard the actual broker fill.

The message names the broker environment, actual side, quantity, price and
execution time, and distinguishes a partial fill from the originally approved
OPEN/ADD/REDUCE/CLOSE. Delayed actual facts remain deliverable with a delay label;
the entry-proposal expiry does not erase an already executed trade. Records
created while delivery is disabled stay SKIPPED_UNCONFIGURED and are not
silently backfilled on later enablement.

The separate `/internal/currency-broker-alerts/{claim,begin,complete,status}`
operations use X-Veritas-Trade-Key and a scoped isolated ledger schema. They
cannot prepare or execute orders. Draining already committed facts requires no
broker token and does not depend on the proposal/execution switches. The sender
checks the actual numeric bot ID and @AxednewsI_bot username, plus channel ID
-1002967459105 and @axednewz username, before claiming delivery. It adds no
getUpdates consumer and places no approval buttons in channel messages.

A committed SENDING barrier precedes Telegram I/O. If sendMessage may have
succeeded but the reply was lost, the event becomes UNKNOWN and is not blindly
resent. Explicit 429 rejection can be retried; an acknowledgement retry cannot
send a second message. UNKNOWN requires operator reconciliation while later
execution facts remain deliverable.

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

FundingReconciler walks all pages for an explicit half-open window [from,to),
without filtering out operation types, commissions, trades or overnight rows.
Funding is operation type 70, with a cost equal to minus the actual RUB payment.
Parent/child attribution must reconcile exactly. Variation margin and already
observed fill commissions are retained for audit and are not posted a second time.
Broker fee type 19 is audit-only because actual order fees already enter the
ledger. Nonzero executed/pending margin fees (14) or other fees (66) attributed
to CNY, or lacking sufficient attribution, block new risk with
NON_FUNDING_ALLOCATION_COST_RECONCILIATION_REQUIRED. They are not mislabeled as
funding or silently omitted from a reconciled allocation. Explicitly canceled,
zero and exactly foreign-instrument costs do not produce that blocker.
Mutable operation IDs are used only to detect inconsistent duplicates within a
read; accepted ledger adjustments identify a versioned window total.

Completing API pagination proves only that the fetch completed. A separate
owner-reviewed broker statement and applicable settlement-calendar attestation
must name the exact snapshot, fill digest, period, allocation, amount, report
hashes, settled-through boundary and next settlement due time. The owner signs
that receipt with the independent statement key after reviewing those records.
This is an explicit owner attestation; the code does not authenticate the
underlying report or claim that the cursor API certifies final settlement.

Windows must be contiguous from allocation binding and cannot overlap. The
service refreshes known windows before reading fresh trade facts. A changed
payment, cancellation, pending operation, late fill, failed/stale history read,
invalid signature or reached settlement boundary blocks new risk. A refreshed
identical economic snapshot retains its attestation; a corrected total requires
new review and posts only its delta. Receipt and delta commit atomically.
Concurrent history attempts use a per-window token: an older response cannot
clear a newer cost blocker or overwrite a newer completed observation.
The 60-second history freshness bound is not a settlement-lag assumption.

Unknown funding prevents high-water NAV increases after actual fills, including
after a profitable close. CLOSE/REDUCE retain their separate reduction path.
An empty API result, one manually recorded funding item, elapsed time or an
unsigned settled=true flag never makes an allocation reconciled.

### Evidence operations

All four internal POST operations require the service key, configured bot,
private-owner identity and exact account/environment scope. They do not approve
or execute a trade.

| Operation | Request and effect |
|---|---|
| `/settlement-status` | Reads settlement storage and returns `bound_at`, `observed_window_end` and the current reconciliation blocker; no broker call or execution |
| `/settlement-observe` | `window_start`, `window_end`; stores a bounded broker-history snapshot and returns a reviewable `receipt_template` |
| `/settlement-attest` | `receipt`, `signature`; verifies independent statement review and atomically records any funding delta |
| `/admission-evidence` | `payload`, `signature`; validates and appends one ACCOUNT_CONTROLS or MODEL_ADMISSION record |

The settlement issuer uses `sign_statement_receipt` after reviewing the external
statement/calendar. The risk/model issuer uses `evidence_scope(approved_terms)`,
`broker_snapshot`, `snapshot_digest` and `canonical_json`; the signature is
HMAC-SHA256 over canonical JSON. Evidence is generated from verified source
artifacts outside this service. No example PASS metrics are production data.
The admission endpoint accepts at most 64 KiB; other trade endpoints retain
their 8 KiB bound. The repository further limits evidence depth, list sizes and
payload size.
Use the exact `bound_at` as the first window's start; later windows start at
`observed_window_end`. The ordinary `/status` remains a cached metadata read.

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
| VERITAS_CURRENCY_TRADE_STATEMENT_KEY | Independent statement-attestation key, at least 32 bytes; missing key blocks post-fill settlement admission |
| VERITAS_CURRENCY_LIVE_EVIDENCE_KEY | Independent risk/model evidence issuer key, at least 32 bytes; missing key blocks production OPEN/ADD |
| VERITAS_CURRENCY_TRADE_SERVICE_URL | Pinned HTTPS service origin used by the Telegram bridge |
| VERITAS_CURRENCY_TRADE_ENVIRONMENT | production or sandbox; bound into every proposal |
| TBANK_API_TOKEN | Existing production broker token, read only from the service environment |
| TBANK_SANDBOX_TOKEN | Separate sandbox token |
| VERITAS_CURRENCY_TRADE_EXECUTION_ENABLED | Currency execution gate; default off |
| VERITAS_LIVE_EXECUTION_ENABLED | Independent global execution gate; default off |
| VERITAS_LIVE_EXECUTION_ARMED | Independent arming gate; default off |
| VERITAS_CURRENCY_TRADE_MARGIN_ALLOWED | Explicit margin permissions; default off |
| VERITAS_CURRENCY_BROKER_NOTIFICATIONS_ENABLED | Separate verified broker-fill channel mirror; default off in both service and bot |

An existing market-data connection or a configured token does not establish that
the selected account is open and FULL_ACCESS. Actual binding verifies the exact
account and a flat position without active CNY orders, and allocates only the
independent Currency capital. This development task does not perform that
private-account binding or change token permissions.

Configuration is an operator handoff, not an activation performed by this branch.
Sandbox and production must never share an account-ledger namespace.
All four service/approval/statement/evidence keys must differ. Constructors and
status remain inert when either evidence key has not yet been configured.

## Architecture audit and remaining operating prerequisites

| Boundary | Resolution or explicit prerequisite |
|---|---|
| CNY potential versus old startup settings | Canonical 1.1× and 0.19% floor win; stale environment values cannot raise the threshold |
| New quote event versus legacy timeframe validator | Canonical event dispatch and actual broker quote rebinding support the current event format |
| PAPER weighted economics versus LIVE execution | Explicit LIVE single-target economics and hard RR checks at both preparation and revalidation |
| Currency limits versus account limits | Both capital scopes are enforced; 10×/35% Currency limits never replace whole-account controls |
| Owner click versus broker acknowledgement/fill | Signed decision, durable one-time claim, broker state and unique execution stages remain separate |
| Funding pages versus settled accounts | Signed external statement review and continuous window refresh supply completeness; pagination alone cannot |
| Status versus execution | Read-only cached status; only the coordinator execution path can claim an approval |
| News scan versus confirmation latency | One bounded background news worker, one getUpdates consumer |
| Paper signals versus actual broker fills | Separate event tables, explicit environment labels, and a transactional broker-fill channel mirror |
| Late cost response versus newer failure | A per-window refresh token prevents stale responses from replacing newer reconciliation state |
| Whole lots versus a small allocation | A proposal below one contract is blocked; neither allocation nor admitted fraction is enlarged automatically |
| Stop/target reference versus broker protection | Closing requires a separate confirmation; an OPEN does not install an automatic protective order |

Operating prerequisites still include explicit numeric owner/bot/account identity,
independent issuer keys, real verified model/risk evidence, reviewed settlement
records, supported complete account valuation and fresh runtime signals. They
cannot be replaced by passing unit tests. Activation and management of an actual
account are not performed by this development branch.

## Validation

The draft CI uses fake broker and Telegram transports and isolated PostgreSQL.
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
Further cases cover signed evidence import/readback, full-account risk, late
funding corrections, changed receipts, blocked high-water updates, explicit owner
binding, slow news processing and current quote-event compatibility. Actual-fill
notification tests cover atomic commit/rollback with the ledger, late fills,
duplicates, cross-environment isolation, pinned Telegram identity, concurrent
claims, and ambiguous sends without automatic duplication.

See the draft pull request checks for the validation result on its current commit.
The repository also retains a separate nonblocking legacy execution audit; its
status must not be presented as part of an unqualified all-tests-passed claim.

## Primary protocol references

- https://www.moex.com/a8141
- https://developer.tbank.ru/invest/services/operations/methods
- https://developer.tbank.ru/invest/services/operations/operations_problems
- https://developer.tbank.ru/invest/services/orders/methods
- https://developer.tbank.ru/invest/intro/intro/token
- https://developer.tbank.ru/invest/services/accounts/users
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/operations.proto
- https://github.com/RussianInvestments/investAPI/blob/main/src/docs/contracts/instruments.proto
