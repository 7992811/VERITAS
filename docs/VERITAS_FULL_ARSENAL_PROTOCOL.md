# VERITAS Full Arsenal Protocol — BTC / ETH

Status: research only. Production `main` remains untouched.

## Objective

Build two independent investment committees, one for BTC and one for ETH. Each committee contains independent alpha modules. A module may vote LONG, SHORT or NO TRADE, but it is admitted only after causal walk-forward validation, realistic costs and an external control period.

The system does **not** search for one universal indicator formula. It seeks low-correlated, economically distinct sources of edge.

## Publicly documented strategy families to cover

1. Time-series trend / momentum — fast, medium and slow speeds.
2. Breakout / channel / compression-to-expansion.
3. Pullback continuation / trend acceleration / phase transitions.
4. Mean reversion / failed breakout / exhaustion / capitulation.
5. Carry — funding, perpetual/spot basis, curve state and basis impulse.
6. Relative value / statistical arbitrage — ETH/BTC residual, rolling beta, lead/lag, convergence/divergence.
7. Cross-market confirmation — BTC→ETH and ETH→BTC leadership.
8. Volatility — realized-vol regimes, expansion/contraction, volatility targeting; options-implied volatility/skew where historical data are sufficient.
9. Derivatives positioning — open interest, taker imbalance, futures volume, top-trader/retail positioning, crowding and deleveraging.
10. Market microstructure — aggressive flow, imbalance, volume acceleration, lead/lag and liquidity proxies; L2/order-book signals only where historical data quality is sufficient.
11. Macro regime — USD, real yields, VIX/financial conditions, liquidity/risk-on-off.
12. Event-driven — CPI/FOMC/NFP, ETF/regulatory/large crypto events where timestamps can be frozen ex ante.
13. On-chain — activity, supply/valuation and exchange-flow metrics only where reproducible historical coverage exists.
14. Defensive / risk-control — volatility scaling, drawdown state, correlation concentration, leverage caps and NO TRADE.
15. Execution alpha — market/limit choice, slippage/impact forecast, spread/liquidity veto, staged entry/exit.

## Non-negotiable BTC / ETH separation

- No shared parameter values by default.
- No transfer of an ETH rule to BTC (or vice versa) without independent validation.
- Direction is validated independently: BTC_LONG, BTC_SHORT, ETH_LONG, ETH_SHORT.
- Each asset can permanently veto a family/direction if the expected value is not proven.

## Evidence hierarchy

A candidate module must pass, in order:

1. Causal features only (no future leakage).
2. Discovery on earlier data.
3. Anchored walk-forward validation on later years.
4. Fee 0.05% per side where applicable + modeled slippage/funding.
5. +5 bp and +10 bp stress where the strategy's turnover makes this relevant.
6. Temporal diversity: not one lucky month/year/regime.
7. External control period not used for parameter selection whenever data exist.
8. Minimum trade count sufficient for the claimed edge.
9. Stability to small parameter perturbations.
10. Only then can the module enter shadow mode; live capital is a separate gate.

## Portfolio decision

Each module publishes:
- direction,
- expected net return,
- conservative EV / confidence interval,
- expected holding period,
- structural stop,
- invalidation,
- liquidity / execution quality,
- regime tag,
- evidence grade.

The council chooses one of:
- LONG,
- SHORT,
- NO TRADE.

Position size is based on conservative EV, evidence grade, correlation between active modules, volatility and portfolio risk. A high model score is never enough by itself.

## Fail-closed rules

- Missing/late/inconsistent data => affected module becomes NO TRADE.
- No positive cost-adjusted walk-forward EV => NO TRADE.
- A module that fails its external control is not rescued by an attractive 2026 result.
- Historical options/on-chain/L2 data that cannot be reproduced are not backfilled with invented proxies and labeled as the real signal.
- Production rules are frozen by commit hash and evidence report.

## Current implementation map

Existing research modules:
- manager library / trend / Donchian / pullback
- phase transitions
- quality-score trend modules
- derivative-state router
- positioning router
- focused microstructure
- multi-horizon trend
- independent BTC/ETH policy
- external 2020-2021 control

Full-arsenal expansion branch adds:
- carry/funding/basis
- relative value / BTC-ETH residual
- systematic mean reversion
- macro regime overlay
- community on-chain adapter
- options-volatility live/shadow adapter where reproducible history is unavailable
- council-level evidence registry and admission gate

## Promotion target

No family is promoted because it is famous or used by a well-known manager.
Only independently positive, cost-adjusted, reproducible evidence earns capital.
