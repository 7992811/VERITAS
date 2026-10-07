# Structural execution latency repair — 7 October 2026

## Scope

Continue issue #99 without changing signal rules, source/contract identity, target/stop levels, commission, leverage, drawdown controls, whole-position risk or PAPER-only authority. Production observations before this repair included 16–23 second structural passes and repeated BUSY. Render CPU quota was 0.15 CPU; observed usage reached that quota. This change does not change the hosting plan or enable broker orders.

## Defects addressed

The entry worker reserved a 20-second next-book turn after BUSY but recomputed native structure for all assets before trying again. That work could itself exceed the reservation. It now retains one bounded pending batch and retries it before history work, with a 0.5-second retry cadence. The authoritative callback still refreshes the execution quote and checks the actual mutation clock, source and complete risk. A retry cannot renew the original event time. A source/contract rollover discards the old queued proposal. A quote refreshed for execution during a retry is not incorrectly marked as already processed for structural detection.

Repeated static proof validation is memoized only after full validation and an unchanged complete-content fingerprint. The cache retains at most 256 SHA256 keys and two scalar result fields, not event graphs. No quote freshness, spent barrier, entry window or portfolio permission is cached. Historical crossing consumption is indexed once per exact native-data bundle with the original bar availability times, rather than rescanned separately for each horizon.

Native dict/list copies avoid a function dispatch for every immutable scalar while preserving Python deepcopy's memo, cycles, aliases, atomic identity and custom-type fallback. There is no serialization or shared mutable data cache in this copier. Independent public and stored event/leg copies remain separate. Only copies that were discarded before use are removed. Market and risk values are not pruned.

The runtime now reports WAITING_FOR_BOOK rather than OK for BUSY, the actual execution reason, retry count, pending count and bounded lock owner/hold-time diagnostics. The original five-second detection target remains a target, not a claimed live response guarantee.

## Isolated validation

- Targeted final run 37684228811 passed, including copy isolation, original CNY prefixes, target lifecycle, source identity, full stop risk, concurrency and reserved retries.
- Full outputs matched the pinned pre-repair engine f848e869361fae0356e8aac66671e291290ece6f in 77 comparisons across all seven horizons, including late/invalid clocks.
- Same-run median context workload: 0.088380 s before versus 0.052454 s after (1.685×). A 40×3500-bar copy workload improved from 0.163648 s to 0.109436 s (1.495×). These are isolated CPU benchmarks, not production latency or profitability.
- Reproduction script: tools/benchmark_structural_equivalence.py. Results: diagnostics/structural-equivalence-benchmark.json. The new permanent Structural Latency Invariants workflow runs the regression suite in addition to mandatory Safety CI.
- One-time patch/export workflows and source transformation scripts are removed from the final tree.

## Production acceptance

Pending deployment and warmed-service measurements. Acceptance requires the exact deployed commit, correct queue handoff, no duplicate execution or source substitution, explicit distinction between BUSY and completed checks, and measured context/book durations. A successful test or an empty warm-up pass is not evidence that all live latency is resolved. Existing user teaching remains USER_INTRABAR_STRUCTURE_2026_10_07; there is no historical fill or profitability claim.
