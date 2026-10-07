# Book accounting I/O — v91.7.23 candidate

## Observed evidence

Runtime observations on 7 October 2026 measured ordinary portfolio accounting
(`_step_one`) at approximately 46–48 seconds and book-lock hold time at
approximately 51 seconds. These are application wall times, not isolated
PostgreSQL execution times.

Two completed v91.7.20 protective passes recorded:

| Measurement, seconds | Pass 1 | Pass 2 |
| --- | ---: | ---: |
| Guard duration | 23.098 | 37.001 |
| Observation writes | 15.515 | 18.799 |
| No-action preflight | 0.333 | 0.504 |

The preflight measurements do not support adding the previously considered
scalar-account cache. No such cache is part of this change.

The observed web-service limits are 0.15 CPU core and 512 MiB; PostgreSQL is
limited to 0.1 CPU core and 256 MiB. These constraints are relevant to the
investigation, but have not been established as the sole cause of the delays.

## Narrow change

Two legacy-only SELECTs in `veritas_portfolio.py` now omit positions their
existing Python loops already skip: the MFE-lock loop in `_v90pi_step_one`
and the R17 `_v90tr_apply` captured by the runtime trailing wrapper.
The shared VTM predicate excludes only an object payload with a nonempty
string `structural_policy_version`. A single strict, silent
`jsonb_path_exists` call avoids repeated extraction of the large payload.
Missing fields, SQL/JSON nulls and other JSON shapes retain the existing
Python path.

This filter does not replace VSL event validation. The first PI read and its
price-discontinuity decisions, predecision/source checks, and R65 source and
candidate work remain in place, including `work.pop(asset)`. There is no
shared position/account cache, combined accounting read, or change to funding,
fees, fills, stop/target authority, or transaction boundaries.

## Scoped diagnostics

Only the `_step_one` call receives the accounting I/O measurement wrapper.
Its detached snapshots contain numeric call/error/row counters, wall times
and current-thread CPU times in a fixed set of operation/table buckets.
They retain no SQL text, parameters, rows, payloads or cursors.
Execute/fetch wall time includes client, transport and server work; fetch
time also includes row decoding. It must not be reported as database CPU
or server execution time.

Book locking and transaction methods remain those of the original connection;
commit/rollback and the subsequent profit-protection refresh use the raw
connection. Instrumentation does not introduce transactions or savepoints.

## Validation and remaining work

**Status: pending CI.** Mandatory native PostgreSQL tests must compare the
same bound helpers with the original unfiltered SELECT, the production
JSONPath predicate and an independent conservative reference predicate.
Acceptance requires identical actions, exceptions, writes and complete
fixture table states, including booked accounts, source metadata and frozen
evidence. Cases include an owned book, mixed LONG/SHORT legacy and owned
positions, missing/null tags, non-object and malformed JSON, and real
trailing/profit-lock calculations. Instrumentation must separately preserve
returned rows, exception identity and commit/rollback behaviour.

The 15-second objective has not been reached. Reduced full-row transfer in
fixtures is not a measured production speedup. A successful CI run must be
followed by new runtime phase/I/O measurements. Memory recovery and completion
of deferred work are not established by this candidate.
