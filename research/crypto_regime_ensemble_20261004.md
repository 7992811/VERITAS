# VERITAS Crypto Regime Ensemble — research checkpoint 2026-10-04

Research only. No production strategy changes.

## Data / protocol
- BTCUSDT, ETHUSDT, Binance Vision 1-minute bars.
- Features derived causally from 1m -> 5m -> 15m -> 1h -> 4h -> 1d.
- Selection / robustness: 2022-2025.
- Evaluation window: 2026-04-04 through 2026-10-04.
- Costs: 0.05% fee per side, 0.025% slippage per side, funding/carry proxy; stress adds 5 bp round-trip.
- Gaps fail closed for 24h after a discontinuity.

## Confirmed research findings

### 1. ETH SHORT — early pullback + BTC risk-off confirmation
Core entry:
- ETH 15m trend SHORT and 1h trend SHORT.
- ETH 4h and 1d must not be LONG.
- A completed 15m bar touched SMA18 within the previous 2 bars.
- 1m close breaks the previous 10-minute low.
- 1m volume >= 1.4x median 20-minute volume.
- 1m bearish body >= 50% of candle range.
- 5m ATR >= 1.10x its trailing 48-bar median.
- Entry extension <= 0.30 ATR(5m).
- Cross-asset filter: BTC 1h return < 0 AND BTC 4h return < 0.
- Stop: previous 30-minute high + 0.20 ATR(5m).
- Target: 2R.

2022-2025 yearly avg trade / PF:
- 2022: +0.189%, PF 1.23, 42 trades
- 2023: +0.024%, PF 1.06, 23 trades
- 2024: +0.118%, PF 1.17, 34 trades
- 2025: +0.145%, PF 1.22, 36 trades
Aggregate 2022-2025: 135 trades, avg +0.131%, PF 1.20, DD proxy 8.52%.

2026-04-04..2026-10-04:
- 12 trades
- win rate 58%
- avg +1.057%
- PF 3.46
- sum of position returns +12.68%
- DD proxy 4.50%
Stress +5bp: avg +1.007%, PF 3.23.

Interpretation: cross-market BTC momentum removed the negative 2024 result seen in the same ETH short setup without cross confirmation.

### 2. ETH LONG — high-quality momentum pullback
Core entry:
- Long momentum vote: at least 2 of 1h / 4h / 1d own returns are positive.
- Completed 15m bar touched SMA18 within previous 4 bars.
- 1m break of previous 5-minute high.
- 1m volume >= 1.8x median 20-minute volume.
- bullish candle body >= 65% of candle range.
- 5m ATR >= 1.25x trailing 48-bar median.
- extension <= 0.15 ATR(5m).
- stop: previous 30-minute low - 0.20 ATR(5m).
- target: 2.5R.

2022-2025:
- 2022: +0.156%, PF 1.22
- 2023: +0.068%, PF 1.12
- 2024: +0.456%, PF 2.01
- 2025: +0.044%, PF 1.07
Aggregate: 219 trades, avg +0.198%, PF 1.34, DD proxy 12.11%.

2026-04-04..2026-10-04:
- 25 trades
- win rate 52%
- avg +0.189%
- PF 1.45
- sum +4.73%
- DD proxy 2.60%
Stress +5bp: avg +0.139%, PF 1.31.

Interpretation: robust positive expectancy across all four selection years and positive 2026, but hit rate is below VERITAS target.

### 3. BTC LONG — risk-on pullback, candidate only
Core:
- BTC 15m and 1h LONG; 4h/1d not SHORT.
- pullback to 15m SMA18 within previous 4 bars.
- break previous 20-minute high.
- volume >=1.5x, bullish body >=55%, ATR5 expansion >=1.15, extension <=0.25 ATR5.
- cross filter with best stability found: BTC and ETH returns positive over both 1h and 4h.
- stop previous 30-minute low - 0.10 ATR5.
- target 1.5R.

2022-2025 yearly avg trade:
- 2022 +0.137%
- 2023 +0.214%
- 2024 +0.199%
- 2025 +0.002%
Aggregate: 128 trades, win rate 51%, avg +0.146%, PF 1.33, DD proxy 5.77%.

2026 evaluation:
- 23 trades
- win rate 57%
- avg +0.095%
- PF 1.25
- sum +2.20%
- DD proxy 5.21%
Stress +5bp: avg +0.045%, PF 1.11.

Interpretation: positive but margin after cost stress is too thin for promotion. Keep research-only.

### 4. BTC SHORT
No robust candidate survived the current 2022-2025 stability filters plus 2026 evaluation. Keep NO_TRADE rather than weaken thresholds.

### 5. Range / failed-breakout mean reversion
A dedicated search over BTC/ETH long/short with 1m wick re-entry at 1h/2h/4h 5m-channel boundaries, low 1h/4h efficiency regimes, volume filters, structural stops and 0.6R-1.5R targets produced zero candidates that met the multi-year stability gate and remained positive after 2026 stress costs.

Conclusion: do not add the current mean-reversion family to the portfolio.

## Current architecture candidate
1. Detect regime first.
2. ETH short: only early pullback continuation with BTC risk-off cross confirmation.
3. ETH long: high-quality momentum pullback / expansion.
4. BTC long: candidate only, reduced priority until stronger cost-stress margin.
5. BTC short: NO_TRADE.
6. No mean-reversion module until a separate robust family is found.
7. Do not chase: late entry distance caps are mandatory.
8. Structural stops only; no stop anchored to the current quote.

## Next research priorities
- Walk-forward ensemble and parameter-neighborhood stability, not single best points.
- Test cross-sectional relative strength / leader-laggard filters without using 2026 to select thresholds.
- Add volatility-targeted position sizing and portfolio overlap controls.
- Test early invalidation / time-stop based on failure to achieve MFE, selected only on pre-2026 data.
- Bootstrap / Monte Carlo trade-order stress and additional cost stress.
- Reserve live shadow forward data as the next genuinely unseen validation layer.
