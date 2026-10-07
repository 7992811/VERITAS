# Protective book latency repair — v91.7.20

## Evidence and release scope

Read-only Render observations on 7 October 2026 found 23 open paper positions
and six quotes per protective pass. On v91.7.16, consecutive completed passes
took 44.620, 64.717 and 82.804 seconds; the corresponding global book-lock
durations were 36.395, 48.406 and 59.311 seconds. On v91.7.17, the passes
completed at 20:09:59 and 20:11:00 UTC took 54.287 and 59.999 seconds,
including 41.587 and 45.206 seconds under the book lock. These are logged
application durations, not isolated database benchmarks. The service has
0.15 CPU and a 512 MiB memory limit. The 15-second protection objective
requires a new production measurement after deployment.

This repair builds on main `0226606`, preserving PR95's fresh post-funding
cost reads, PR96's structural stop correction, PR97's entry retry priority,
and PR98's snapshot revision and bounded portfolio response changes.

## Changes

- Extract the quote identity fields from the JSON root once. Preserve all
  original keys, explicit nulls, source locks, contract identity and quote times.
- Read only fields used before protective execution. Keep the complete frozen
  structural events; preserve the distinction between a missing field and an
  explicit null. Financial execution reloads the complete position from the
  current locked portfolio before funding and canonical accounting.
- Group observation patches for positions whose checks prove no action under
  both successful and failed observation persistence. Structural and legacy
  positions use their existing decision rules. A legacy preflight account read
  is never reused as the authoritative account read for an action.
- Write at most 16 distinct trade identifiers per group with two SQL updates.
  A failed optional group rolls back, then retries its members independently.
  An individual metadata failure cannot suppress a real stop or discard the
  other positions' observations. Duplicate identifiers retain sequential
  `jsonb ||` semantics, including legacy non-object payloads.
- Batch the already calculated profit-protection refresh patches. A required
  second-table failure propagates and rolls back the outer accounting
  transaction. Fees, funding, fills, target stages, source eligibility and
  canonical accounting formulas remain under the established locks.
- Measure observation writes, no-action preflight, legacy profit locking and
  profit refresh separately so remaining production delay can be attributed.

## Verification contract

Mandatory PostgreSQL 18 integration compares the new pass with the frozen
`0e4ace8` guard using the same current financial functions and synthetic
LONG/SHORT fixtures. It compares the full portfolio, position, trade, order
and NAV tables for no-action observations, real stops, two stopped assets in
one portfolio, four days of funding, partial targets and final targets.
It also covers optional per-row failures, required paired-write failures,
outer rollback, original evidence retention, stale/future/foreign quotes,
absent/null/malformed JSON, and old/new quote-projection parity.

The 23-position no-action fixture requires the same final book with no more
than four observation updates, versus the original 46. This statement count
is not a claim of a particular production speedup. Venue/contract projection
tests execute the actual query in PostgreSQL instead of a SQLite adapter.
The existing required safety stages and architecture ceilings remain intact.

This release does not raise server limits or memory admission thresholds.
Post-deployment verification must report actual protective durations and any
remaining deferred maintenance explicitly. Synthetic fixtures demonstrate
accounting consistency; they are not evidence of historical profitability.
