# R79: new continuation entries after a spent breakout

The observed MOEX 4h Strong Long snapshot still used the 2297.16 trigger from
07:26 UTC. Its 2309.64195 original target was consumed, so timing and geometry
both rejected entry despite a valid higher-timeframe direction.

Changes:

- A three-candle closed 5m pause can form a separate continuation event. It
  requires a counter-directional candle/close, a bounded range, progress beyond
  the previous trigger, and a subsequent observed crossing with activity.
- The event owns its fixed trigger, actual confirmation timestamp, local
  invalidation stop and fixed target projection. Price polling cannot refresh
  that event or move its target. The existing full-base detector remains active.
- A new continuation can replace a parent plan's expired-target/timing status.
  Source, quote freshness, hard invalidation, history, reentry, portfolio risk
  and post-cost economics remain authoritative.
- Both final admission and the model portfolio write path measure technical
  extension from the observed quote. Simulated adverse fill is still fully
  included in economic and risk calculations.

Validation:

- 70 targeted tests passed, including both directions, all four model books,
  the final write boundary, unchanged fee/move policy, stale quote rejection,
  adverse-fill costs, no future candles, fixed identities/targets and R78
  catalyst compatibility.
- A broader 237-test comparison has identical baseline and patched results:
  82 existing failures and one existing error, with no new failure IDs. These
  older tests include obsolete contracts and are not claimed to pass.
- The attached observed MOEX minute fixture detects a distinct continuation at
  12:03 Moscow time, using a 2310.25 local trigger. Future candles cannot change
  that historical decision. This validates event detection, not profitability.
- Minute-by-minute replay of the supplied MOEX morning slice produces no fully
  admissible entries before or after the patch under current costs/nearest-level
  rules. The new detector removes obsolete-event vetoes in some observations,
  but remaining reward/risk and cost vetoes still apply. No profitability or
  real execution claim is made from this replay.

Scope: existing model portfolios only. No broker integration, real order,
historical portfolio rewrite or direct database mutation. The requested 0.19%
minimum potential and 1.2x cost buffer are unchanged.

## Integration with main on 2026-10-05

Main advanced to a1d734d while approval was pending. Its separately released
R79 signal-authoritative policy and CNY quote changes are preserved. This
supplement does not undo or further relax that policy. The shared timing
conflict was resolved by using observed prices for every setup, including
displayed-signal and catalyst setups.

After integration, 68 of the 70 targeted tests pass. The two failures are also
present in the 59-test unchanged-main baseline: the R74 observed CNY target
assertion and the old catalyst context-expiry assertion. All 11 new continuation
tests pass, with no new failure relative to current main.
