# VERITAS v70.7 — Intelligence + Portfolio Control Center

Key changes:
- Champion admission floor 70%; Challenger 77%.
- Fast Tactical Reversal engine for early counter-trend entries on 1h/4h.
- Tactical reversal starts at 5–15% NAV, requires >=70% model prior and R/R >=1.30; structural confirmation is required before scaling beyond tactical size.
- Support/resistance engine plus SMA18/SMA50, slopes, distances and structural bias.
- Portfolio accounting UI: notional, units, average entry, current price, stop, unrealized P&L, exit price and realized cost decomposition.
- Opportunity Radar 2.0 with entry, stop, expected move and R/R.
- Decision Depth Score replaces misleading “30” depth label; 30 remains only matrix cells (6 assets × 5 horizons).
- Learning Index shows preliminary sample progress instead of only “building”.
- Fast overview now exposes Knowledge Factory and research discovery health.
- Alert cards show action-oriented interpretation.
- Manager discovery coverage broadened; new material remains shadow until validation.

Guardrails:
- Paper only; no broker/live-capital execution.
- Existing portfolio history is preserved.
- Tactical reversal never bypasses hard drawdown, stop-risk or portfolio gross-exposure controls.
- Probability source is explicitly labeled; model priors are not presented as empirical hit rates.
