# VERITAS Real-Account Admission Gate

Status: fail-closed. A real account remains disabled unless every mandatory gate below is explicitly PASS.

## 1. Research evidence

For BTC and ETH separately:

- Causal signals only; no look-ahead, no future-labelled features at decision time.
- Entry is executable after signal availability, never at the signal's already-known price.
- Commission: 0.05% per execution side.
- Slippage: realistic state-dependent estimate; must also pass an additional +5 bp round-trip stress.
- Funding/basis included where the traded instrument requires it.
- At least 100 independent completed trades for the promoted directional policy, or a statistically justified lower count with a wider confidence haircut.
- Positive net expectancy over multiple market regimes, not only one bull/bear year.
- No single calendar year may account for more than 50% of total net profit.
- Profit factor >= 1.30 base and >= 1.15 under +5 bp stress.
- Target win rate >= 65%. A lower win rate is not promoted merely because one period has large winners; it remains research-only unless the investment committee explicitly changes the mandate.
- 95% block-bootstrap probability of positive cumulative P&L >= 95%.
- 95th-percentile bootstrapped drawdown <= 15% for normal portfolios; <= 20% only for Aggressive.
- Parameter-neighborhood stability: small changes in thresholds/stops must not flip expected value deeply negative.
- Multiple-testing defense: promote broad parameter plateaus / theory-led fixed rules, never an isolated optimum.

## 2. Regime robustness

- The policy must update causally using only past completed episodes.
- If a strategy is regime-specific, regime identification must itself be causal.
- Regime edge must survive at least one external period not used to select that regime.
- Edge degradation automatically changes the strategy to NO TRADE.
- Missing derivative/on-chain/macro inputs cannot be imputed as favorable; affected modules fail closed.

## 3. Portfolio risk

- BTC and ETH have independent models and evidence.
- Per-trade monetary risk is derived from the structural stop, not model confidence alone.
- Same-direction BTC/ETH crypto-beta concentration receives a correlation haircut.
- Market-neutral carry and pair-relative-value are separate sleeves.
- No averaging down a losing thesis.
- Adding to a position is allowed only after price/structure confirms the thesis.
- Portfolio gross/net exposure, leverage and per-asset limits are checked before order creation.
- Aggressive leverage up to 1:5 is technically permitted only after the underlying unlevered strategy itself passes all evidence gates.

## 4. Execution and state integrity

Before every order:
- one authoritative market-data source for the execution decision;
- quote freshness PASS;
- timestamp synchronization PASS;
- spread/liquidity PASS;
- position state reconciled with broker/exchange;
- idempotent order key generated;
- stop/TP state persisted before/atomically with live order acknowledgement where supported;
- stale or contradictory data => NO TRADE.

After every fill:
- actual fill price, fee, slippage and funding recorded;
- broker position reconciled;
- partial fills handled explicitly;
- stop/TP quantities recalculated from actual filled quantity;
- no duplicate order on retry/restart.

## 5. Shadow / paper gate

After research PASS and before real capital:
- frozen commit hash;
- minimum 30 completed shadow trades AND minimum 30 calendar days;
- same real-time data path intended for live trading;
- no manual cherry-picking or retrospective signal edits;
- realized shadow PF >= 1.20;
- positive realized net expectancy after actual observed spread/slippage assumptions;
- no unexplained missed/duplicate orders;
- no position-state disappearance;
- all stop/TP/reconciliation tests PASS.

## 6. Limited-capital gate

Only after shadow PASS:
- initial live risk budget <= 0.10% NAV per trade;
- no leverage;
- daily loss limit 0.50% NAV;
- weekly loss limit 1.50% NAV;
- automatic kill switch on data/execution/reconciliation faults;
- automatic return to shadow on statistical or operational degradation.

Capital may scale only after a separately documented live review.

## Current status

REAL_ACCOUNT_ALLOWED = FALSE

Reason: no BTC/ETH directional policy has yet passed the full causal + external + stress + shadow evidence chain. This file is a safety gate, not a claim of profitability.
