# VERITAS Execution Quality v3

Paper-only release; no broker order methods, no live-execution activation.

## Direct CNYRUBF

`VERITAS_CNY_PRIMARY_SOURCE=TBANK` is the default. The existing read-only T-Invest
connection supplies the exact perpetual CNYRUBF UID, MOEX venue, native closed
candles, quote, order book, and trading status. Contract tick/size validates RUB
per CNY units. No automatic substitution by a dated CNY futures contract is
allowed. An optional MOEX observation is verification only, never the fill.

Missing or stale broker data is visible as a research-only state. MOEX history
can remain visible but cannot open paper risk while TBANK is selected. This is
not evidence that a broker token is configured or that the live feed passed.
The final feed state is exposed by `/api/v1/strategy-quality` and existing
`/api/v1/tbank/status`. Positions retain their entry provider and exact UID.

## Economics and portfolio roles

Minimum move is `max(0.19%, 1.1 * modeled round-trip costs)` in every paper portfolio,
including Currency. Commission and modeled slippage are 0.04% per side. The entry
multiple is a compatibility alias for the same owner-approved 1.1x buffer; the
constitution rejects drift between these values. The cost buffer does not replace
the separate net reward/risk check. No SUPER exception bypasses negative net
economics. Portfolio capital, gross, drawdown and stop-risk limits remain.

Impulse selects early 1m/5m/1h setups. Aggressive requires building/confirmed trend
structure. Champion is the confirmed-trend control. Challenger adds a local
trigger and is compared only on matching explicit market events. Currency has
its own exact-instrument route. These are proposed paper policy distinctions,
not demonstrated trading edges.

Within each role, candidates are tried in their existing priority order through
complete canonical admission. A blocked high-ranked timeframe cannot hide an
independently admissible lower-ranked timeframe. If all candidates fail, the
highest-priority candidate and actual rejection trace remain visible. Final
admission rechecks the current quote, drawdown and held-position constraints.
Legacy R20 SUPER selection cannot replace a canonically routed book.

Before runtime admission, a newer cached quote may replace an aged observation
only on the same provider and pinned contract. Its original exchange timestamp
is preserved, and price geometry plus the 30-second crypto execution limit are
checked again. Actual entry uses one final clock through admission and accounting.
Explicit historical clocks remain deterministic unless a runtime row explicitly
requests quote refresh. HTTP refresh results are checked at the clock after the
request returns; slow analysis is not permission to relax quote-age limits.

TP remains partial where the 5% position step permits a runner. A confirmed
strong trend may retain up to 70%. Source, net-profit and structural stop checks
remain required. The old R69-specific profit-lock disable is removed only for legacy positions.
New positions under CTC_SAME_TF_STRUCTURE_V1 retain the later owner instruction:
all stops and trailing swings use the entry timeframe; synthetic profit locks
remain disabled for those tagged positions. Stops do not guarantee an exit price
or a profit.

## Immutable evidence

New trades stamp `strategy_epoch=EQ3_2026_10_06`, entry SHA, policy hash, role,
market idea identifier and source identity only in the new-trade INSERT branch.
Adds, restarts, report reads and reviews do not relabel existing trades.

The dashboard retains the original all-history NAV and ledger, alongside a
separate evidence panel: all history; entries since verified baseline deployment
73266d9 (2026-10-06 19:16:36.463729 UTC); current epoch; last 20/50 event groups.
Legacy time-based baseline attribution is explicitly an estimate, not an
invented entry SHA. Inherited open positions remain in accounting and are counted
separately. Repeated allocations across portfolios do not create four market
ideas. Event clustering does not establish statistical independence.

Capture uses entry notional and observed favorable excursion, not portfolio NAV.
It stays unavailable when the excursion/path is missing, sources are invalid,
or multiple entries make the aggregate basis ambiguous. Short inverse telemetry
is converted to linear return units. No missing data is replaced with a success.

A bounded background review updates only `paper_trades.payload.posttrade_review`
for closed trades. It is input-hash idempotent and does not change financial
columns, strategy parameters, historical results or broker orders. Recommendations
are shadow-only. There is no automatic promotion or guaranteed win rate.

The snapshot labels staleness, history truncation, estimated groups, matched
sample count and insufficient evidence. A positive small sample does not enable
real-money trading. PostgreSQL regression tests run in an isolated CI database.
