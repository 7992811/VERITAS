# VERITAS Observation Sidecar v91.8.34

The observation sidecar exists only to preserve causal paper-trade evidence.

It runs independently of the heavy protective-management loop. Every 10 seconds
it reads active positions without a book lock, selects only already-published
pinned quotes from the in-process cache, updates one bounded witness per trade in
`paper_observation_sidecar`, and writes nothing to orders, positions, P/L, risk
limits, stops, targets or sizing.

## Causal boundary

- A witness is seeded only after canonical accounting confirms the original fill.
- A carried position without a sidecar entry seed never receives a fabricated
  historical prefix.
- The sidecar never fetches a quote from the network.
- Provider, exact contract, direction and timeframe remain pinned.
- A service restart or a missed sidecar cadence remains an evidence gap.
- Existing historical trades are never backfilled.
- The exact exit quote seals the witness immediately before close/reduce.
- If the sidecar is absent or fails, the existing observation-path writer remains
  the fail-closed fallback.
- The witness is research evidence only and has no production-trading authority.

The design intentionally separates evidence continuity from protective strategy
latency: a delayed stop-management pass must not destroy future learning evidence
when a lightweight cached quote sample could have been recorded independently.
