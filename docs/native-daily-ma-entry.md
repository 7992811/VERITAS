# Native daily averages, rebound entries and stable Brent source

## Owner intent and scope

The owner requested daily SMA50/SMA200 as potential support/resistance and
confirmed rebounds as an additional entry scenario. This supplements the
previous same-timeframe structural-breakout instruction. All five configured
paper portfolios use the common canonical entry path; their asset, timeframe,
trend/evidence and risk limits remain separate admission requirements.

The original teaching ledger record is unchanged. The new instruction has the
separate key `user_teaching:USER_DAILY_MA_REBOUND_2026_10_07`.
This is an operational policy, not an ML training result or a validated edge.
The strategy epoch is `EQ4_2026_10_07_NATIVE_MA` to keep evaluation evidence separate.

## Daily data

SMA18, SMA50 and SMA200 use actual completed native daily candles. Each period
has its own availability status: 60 daily bars can provide SMA50 but cannot
provide SMA200. Hourly row groups, partial days, conflicting observations,
another instrument or another venue are not valid daily input.

Sources match the selected execution identity:

| Execution source | Native daily data |
| --- | --- |
| Binance spot BTC/ETH | Spot klines, interval 1d |
| ProFinance NQ/GOLD/BRENT | Native Last-price daily history with observed successor-day completion proof |
| MOEX IMOEX/CNYRUBF | ISS candles, interval 24, exact security |
| T-Invest CNYRUBF | Existing read-only D1 candle snapshot, exact UID and price normalization |

The bounded cache is shared by execution timeframes, preserves source identity
and reports missing/stale data explicitly. No broker order RPC is involved.

## Rebound sequence

1. Price approaches the daily average from a clear side.
2. A completed candle of the selected execution timeframe touches its zone.
3. Freeze the daily MA value, period, availability time and source proof before
   that touch. Later daily closes cannot move this episode's level.
4. A subsequent completed local candle reclaims/holds the average and closes
   beyond the local touch high for LONG or touch low for SHORT.
5. Set the stop beyond the preceding local episode extreme, with that
   timeframe's ATR buffer. Project the target using the same local ATR/risk.
6. Apply the unchanged freshness, anti-chase, source, cost, net reward/risk,
   event-reuse and portfolio checks at the actual modeled fill.

The first confirmation consumes the episode even if admission later fails.
Repeated polling or another crossing inside that episode cannot restart its
clock. Rearming requires a separate observed price excursion.

Missing daily data disables the MA scenario alone. Structural breakout
candidates remain available. When both exist, current timing and modeled cost
admission take precedence, followed by original confirmation time.
Confirmed aligned trend state survives a fresh trigger.

## Parameter status

These are explicit, unvalidated implementation defaults:

| Parameter | Default |
| --- | --- |
| Daily MA periods used for rebound | 50, 200 |
| Touch half-width | 0.10 daily ATR |
| Clear approach / rearm | 3 local bars beyond 0.25 daily ATR |
| Maximum touch episode | 12 local bars |
| Daily slope measurement | 5 completed days, normalized by daily ATR |
| Maximum adverse slope | 0.25 daily ATR |
| Flat/repeated-crossing filter | Flat slope within 0.10 ATR and at least 3 crossings in 10 days |
| Supported rebound execution TF | 1m, 5m, 1h, 4h, 1d |

Common structural risk parameters retain ATR20, stop buffer0.15 ATR,
maximum stop3 ATR, extension0.5 ATR, age1 local bar, and initial target
max(2R,1.5 ATR). These numerical choices require independent post-cost,
out-of-sample evaluation.

Without a full exchange calendar, daily data expires after four calendar days
(one for continuous Binance), plus60seconds. Long exchange holidays can make
the MA scenario unavailable until another actual daily close is received.
No artificial holiday bars are inserted.

## Brent repairs

New normalized BRENT candidates use ProFinance for both OHLC and quote.
MOEX discovery no longer silently selects a delayed contract or switches the
entry price basis between near contracts. A temporary ProFinance failure
produces an explicit unavailable/stale state.

Existing positions retain their entry source and exact contract, including
MOEX positions. Their guarded mark/exit route is unchanged.

Native1m/5m history is requested before slower history within the existing
bounded budget. Already attached native history bypasses redundant Yahoo
minute retrieval and duplicate history retrieval. Partial hourly research
data is explicitly marked and cannot crash independently valid minute
processing or manufacture missing returns.

## Costs and validation

The later owner-approved entry threshold remains
max(0.19%,2.0 * modeled round-trip costs). Commission and paper slippage remain
0.04% per side each. The earlier1.1 base-buffer setting is not the final entry
threshold. The policy text is synchronized with the actual2.0 threshold.

Safety CI requires native-provider/source, causal MA, event lifetime,
same-timeframe geometry, full canonical admission for all five portfolios,
partial-hourly and existing accounting/risk regressions.
No profitability conclusion follows from passing implementation tests.

## Daily-context runtime validation

A per-evaluation index parses and validates the native daily observations once.
Each requested snapshot still selects only observations available at its exact
historical time, including late arrivals and conflicting duplicates. The full
local candle walk and consumed/rearmed episode state are retained.

Regression tests compare complete indexed contexts with the original uncached
calculation, including provenance digests, every supported execution timeframe,
both directions and both MA periods. The 500-day case also verifies that an
old consumed signal survives 252 subsequent touches without receiving a new
identity or time. Operation counts are deterministic; elapsed test time is
reported as a measurement, not a machine-dependent pass threshold.

This optimization keeps the entry policy, source checks, cost limits, strategy
epoch and immutable owner teaching snapshots unchanged.

## ProFinance daily completion evidence

The date-only ProFinance daily response does not certify a physical session
closing time. A date label is retained as a nominal ordering index. The latest
native daily record is excluded; an actually observed later valid native date
certifies the preceding record. The certificate binds source, both date labels,
OHLC digest and first observation time.

Repeated unchanged data keeps its first certificate. A changed certified OHLC
has a new availability time and a revision watermark. Historical snapshots
before that watermark are explicitly unavailable when the old revision cannot
be reconstructed; the calculation cannot silently substitute an older day.
A refresh that loses a previously certified date within the retained range is
rejected while preserving the prior complete snapshot and its original age.

Daily MA availability uses the real certificate observation time. A historical
touch before the first certificate cannot become a new signal at fetch time.
Future approach, touch and confirmation candles can use the certified levels.

For these date-only inputs, diagnostics expose the provider period label,
completion observation and nominal-date basis. A verified physical close time
remains null. Date-only native records cannot supply structural D1/3d/7d event
timestamps. Independently complete observed hourly buckets may still define
explicit fixed UTC execution intervals; these aggregated intervals never
replace native daily input for SMA calculation. Other providers retain their
verified native interval boundaries.
