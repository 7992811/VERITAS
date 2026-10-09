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


## Health and bounded storage (v91.8.35)

Sidecar telemetry distinguishes pre-deploy/carried positions from genuinely
entry-seeded positions without exposing trade IDs:

- seeded_positions;
- observed_positions;
- unseeded_positions;
- seeded_events and sealed_events.

Expected old unseeded positions no longer force a log every 10 seconds. A
snapshot is emitted on material health-shape changes, missing quotes, or at
least once per minute.

Closed-trade sidecar rows are removed in bounded batches of at most 32 once the
corresponding active paper position is durably absent. Cleanup does not hold the
paper-book lock and cannot delete evidence for an active position.


## Cold-cache semantics (v91.8.36)

A temporarily absent cached pinned quote does not mutate a previously committed
witness. In particular, service startup is allowed to warm the quote cache
without incrementing invalid_observation_count.

This does not relax continuity. The next valid sample compares its check time
with the last committed valid check. If that real gap exceeds the existing
45-second limit, the path still becomes permanently incomplete. A stale,
mismatched, malformed or wrong-source quote is still passed to the strict path
validator and remains invalid evidence.
