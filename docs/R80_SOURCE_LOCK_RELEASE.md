# R80: immutable position price source

The 2026-10-05 GOLD incident demonstrated that an asset symbol is not a quote
identity. A ProFinance entry was subsequently marked and closed using a GLD/GC
proxy. This release binds each paper position to its entry provider/channel and,
when recorded, its exact contract.

The lock applies to entry/addition, main portfolio NAV and marks, MFE/MAE,
protective monitoring, closing fills, and the common dashboard enrichment path.
Per-source quote caches allow two portfolios holding the same asset from
different providers to coexist. A different provider cannot become a fallback
for an existing position or supply its thesis-invalidation row. STOP is checked
again against the pinned quote at the final accounting boundary.

If the original source is unavailable or stale, retain the last source-verified
mark, show its timestamp and unavailable status, and do not simulate an exit on
another feed. On legacy positions with no verified mark, use the recorded entry
reference. Provider inference is restricted to historically fixed MOEX and
Binance adapters; GOLD/NQ/BRENT are never inferred from the current feed.

## Confirmed GOLD incident

- Trade: `Impulse:GOLD:1791207888095`, paper SHORT, 1h.
- Entry: 2026-10-05 13:44:47 UTC (16:44:47 Moscow), ProFinance 4139.70;
  simulated fill 4137.505959, quote observed at 13:44:09 UTC.
- Exit: 14:00:04 UTC (17:00:04 Moscow), GLD/GC proxy reference
  4158.875699230643; simulated fill 4161.08147689603.
- Reason: `HARD_THESIS_INVALIDATION`, not STOP.
- Recorded stop: 4182.2998046875, local Yahoo GC=F resistance. Its basis was
  not reconciled to the ProFinance entry. This release does not retrospectively
  reprice that stop or claim that historical Yahoo levels represent ProFinance.
- Recorded net result: -1310.9990917401078 RUB, retained unchanged. The exact
  same-source quote at the closing time is not available, so a corrected P&L
  must not be invented.
- Prior execution attempts were blocked; the original breakout event was at
  13:06 UTC. Generic historical `EXECUTION_CONTROL_BLOCKED` audit records do not
  establish every individual blocking reason.

The affected GOLD episodes in Impulse, Aggressive, Champion and Challenger are
marked `MIXED_PRICE_SOURCES` and excluded from learning. Existing sanitation
rebuilds learning from eligible evidence without changing the financial ledger.

## Verification

72 targeted Python regressions cover source replacement, stale/missing quotes,
mixed-provider books, explicit contract changes, final fills, false/true stops,
dashboard marks, entry audit and learning exclusion, alongside prior execution,
continuation, quote-protocol and profit-protection behavior. The JavaScript UI
suite checks provider/status display and prevents signal-price substitution.

The lock identifies the provider/channel. Public continuous feeds that do not
expose an exact expiry remain paper instruments; their exact futures contract is
not established by this change. Historical indicator/level construction is not
recalibrated in this release.
