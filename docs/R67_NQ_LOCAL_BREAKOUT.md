# R67 — original local NQ breakout and execution timing

## Reported incident, 2 October 2026

The public NQ=F candle series matches the submitted chart's local resistance at
31,035.50. The 12:30 UTC minute closed at 31,047.25; its containing 5m candle
closed at 31,118.25. The pre-impulse low was 30,913.25. Historical candles alone
do not demonstrate when the provider delivered those values live.

The old paper execution selected a 4h thesis, measured lateness against a later
31,151.50 level with a 1% floor, and had no originating local event. Its research
price came from a QQQ proxy bridge. Mixing proxy bars and irregular quote-anchor
timestamps into the closed-bar series could make the R66 context insufficient;
the missing event then admitted the legacy path.

## Rules now enforced

- NQ structure uses direct, grid-aligned, completed 5m bars. Completed groups of
  five direct 1m bars advance cached history. QQQ bars and quote anchors cannot
  become structural candles. Missing context blocks new NQ risk in all books.
- Prepare the upper/lower boundary of the preceding 72 5m bars (minimum 36),
  with at least two separated touches within half a pre-break 20-bar ATR.
- A completed 1m crossing can originate the event before the 5m close. A fresh
  direct paper quote can provisionally cross an already prepared boundary,
  within 0.1–0.8 ATR. A QQQ-derived price cannot confirm an NQ entry.
- A large crossing candle is remembered even if too extended to enter. The
  original level, identity, ATR and stop persist through subsequent highs.
  The scan retains up to four hours; two closes back through the level invalidate
  an event. The implementation never uses an oversized-candle filter to erase
  a missed initial breakout and manufacture a later new one.
- Initial execution requires no more than two elapsed 5m periods, or a recent
  completed retest; adverse modeled fill must remain within 1.5 original ATR.
  Quote freshness and closed-context freshness remain independent checks.
- The stop is beyond the pre-crossing 60-minute impulse extreme by 0.15 ATR.
  A 1h/4h thesis cannot replace it with its own wider stop. Size is rounded down
  against the existing stop-risk budget including modeled adverse fills/costs.
  Qualified Aggressive initial 50–100% and its 500% ceiling are preserved.
- The nearest known H1/H4 obstacle bounds the target. Forecasted distant targets
  cannot justify skipping it. Existing post-cost economics gates still apply.
- Adds require a distinct profitable confirmation and sufficient remaining room;
  stops on existing positions are never widened by this release.
- Admission diagnostics include original level and extension in ATR, with Russian
  explanations for missing context, proxy prices and waiting for a breakout.
- The independent paper protective guard also checks fresh ProFinance
  NASD100_FUT quotes instead of relying solely on delayed Yahoo minutes. It
  rejects cash-index labels and stale quotes; it does not substitute this
  continuous reference for a position carrying a specified contract identifier.

These are fixed implementation thresholds, not proven optimal parameters.

## Reproduction and limits

```
python tools/replay_nq_breakout.py
python -m unittest test_veritas_trend_entry test_veritas_local_breakout test_veritas_execution_repairs test_veritas_profit_protection test_veritas_single_source test_veritas_trade_view
node tools/test_paper_ui.cjs
```

The case fixture contains public candles only, no account or position records.
The replay uses completed observations, the same geometry/fill/cost functions,
75% initial notional, and records the result in `docs/research/r67_nq_case.json`.
Tests include prefix invariance, symmetric Shorts, scaled prices, missing context,
synthetic bars, stale history, all-book admission, actual order rejection,
structural stop preservation, and retained/capped initial allocation.

At 12:31 UTC the timing rule qualifies, but the first known H1 obstacle is
31,151.50 against a structural stop around 30,909.83. Gross R/R is approximately
0.759 and modeled net R/R at 75% allocation is 0.176: no order qualifies.
At the reported late price around 31,205 the timing rule rejects the entry,
even though the older distant-target economics could appear attractive.

This is a selected incident regression, not an out-of-sample profitability
claim. It does not establish that the free delayed feed could have filled the
first crossing live. Public quote freshness does not certify an executable
exchange order book or exact contract. This remains normalized paper trading;
live capital execution is unchanged and disabled.
