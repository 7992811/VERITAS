# VERITAS v70.8.4 — Range Participation / Retest Before Breakout

## Purpose
Prevent the system from missing strong moves that begin from a valid retest/support bounce before a visible range resistance is broken.

## New state machine
- RETEST_ENTRY: small 5–15% NAV paper position after a qualified support/retest hold.
- APPROACH_RESISTANCE / APPROACH_SUPPORT: manage existing participation position; reduce to a small runner when the range edge is reached without impulse. Do not open a new late position at the edge.
- BREAKOUT_ADD: after an impulsive, volume-confirmed break of resistance/support, increase exposure to 20–30% NAV in 5% portfolio increments.

## Stops
Participation stop uses the nearest valid tactical support/retest structure plus a volatility/noise buffer. The wider strategic stop remains a separate core-thesis level and no longer blocks a small participation entry.

## MOEX intraday structure
MOEX now carries a 5-minute research context from the existing Yahoo IMOEX.ME secondary feed while the official MOEX quote remains the primary price. This intraday context is research-only and does not make the feed execution-grade.

## Paper source policy
Normal paper candidates still require execution eligibility. A qualified tactical reversal or range-participation setup may enter RESEARCH_ONLY_PAPER with one direct source only when source_gate_pass=true. This exception does not apply to live capital execution.

## Learning
Added zero-weight durable case lesson MOEX_2026_09_22_RETEST_BEFORE_2300_BREAKOUT and ongoing setup_learning events for future independent validation.

## Tests
16/16 tests passed, including:
- retest entry near support;
- tactical stop beyond support zone;
- partial-risk state near resistance;
- impulse breakout add;
- research-only paper admission limited to qualified tactical setups;
- prior Brent impulse-breakdown and CNYRUBF regression tests.
