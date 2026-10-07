# Book storage latency repair — 2026-10-07

## Measured problem

The accounting instrumentation from PR #109 measured two committed books on
production SHA `1654a6855b7ef2e362afe4a3841bdf9e11eda6f7` (instance `lrwhz`).
Impulse took 34.595 seconds, including 21.159 seconds of position reads and
9.553 seconds of position/trade writes. Aggressive took 27.299 seconds, with
13.776 seconds of position reads and 9.731 seconds of position/trade writes.
Execute plus fetch accounted for 95.5% and 92.1% of accounting wall time.
These are driver wall times, including server, transport and scheduling, not
measurements of pure PostgreSQL execution time. Current-thread CPU was only
0.459 and 0.672 seconds. Both books committed without measurement errors.

The next automatic release, SHA `9f3bcd8b0a5a00050e48fa5b2efbd7f9a859cd79`
(PR #105, instance `kf2nv`), retained this accounting code. At 21:25–21:26 UTC,
Impulse accounting took 30.603 seconds and Aggressive 42.602 seconds. Position
reads took 15.214 and 16.767 seconds; position/trade writes took 11.599 and
21.460 seconds. These observations differ in workload and are not a controlled
speed comparison. They confirm that the remaining bottleneck is still present.

After the first two books, the protective guard waited 22.3 seconds for the
Python book lock; complete passes took 37.212 and 35.099 seconds. The 15-second
target had not been achieved. The lock is retained because all accounting for
one portfolio must commit or roll back together.

## Changes

1. R19 skips only its second, flip-specific whole-book read when the prepared
   candidate book is empty. Its first management/trailing pass remains active.
   A nonempty candidate book still reads fresh positions after trailing.
2. R56 retains the original migration call and all preceding validation. When
   `VTM.owns_position(z)` then proves that both legacy migration and backfill
   are no-ops, the wrapper omits the individual full-payload reread. Legacy
   positions retain the fresh post-migration read and TP backfill. NAV still
   includes the full original book.
3. The `PORTFOLIO` and `PROTECTIVE` lanes of the existing book transaction use
   `veritas_book_storage.configure` once, after acquiring the advisory lock.
   Other lanes, including structural entry, do not run this optimization.
   The helper checks the actual connection's compression capability and both
   resolved payload-column policies. Only a compatible server and default,
   compressible JSONB columns permit `SET LOCAL default_toast_compression=lz4`.
   Explicit column policy is preserved. No table alteration or historical
   rewrite is performed. The transaction's small diagnostic records the actual
   decision and setup wall time separately from accounting.

The storage helper executes its probe within one nested savepoint. An unsupported
SET value (`22023`) falls back only after savepoint exit and confirmation that
the original connection remains open, usable and `INTRANS`. Catalog, connection,
permission, timeout and other errors retain the ordinary book failure/rollback
path; they are not reported as unsupported compression. Normal outer commit or
rollback restores the previous session setting. There is no manual reset or
outer rollback inside the helper.

`VERITAS_BOOK_TOAST_COMPRESSION=default` disables the additional SQL. The default
request is `lz4`, subject to the actual checks above. The GUC can affect other
default-compression values written in the same transaction, including order
payloads. Logical values, constraints, financial operations, source/contract/time
evidence, fees, funding and trading rules are unchanged.

## Isolated evidence and storage tradeoff

Research PR #113, exact head `1c626d2660da6e2b8bbf3f24abfea4d34357125a`, passed
[Safety CI 37687593847](https://github.com/7992811/VERITAS/actions/runs/37687593847)
with 2,055 test executions and no failures or skips on PostgreSQL 18.6. Its
native experiment is retained in this release's mandatory PostgreSQL stage.

The experiment uses the actual `PIO.write_patches`, 23 varied synthetic payloads
per table, two logged tables, 158,379–162,789 JSON bytes per row, one transition
warmup and three measured batches. Median writer wall time was 0.164439 seconds
with pglz and 0.043776 seconds with LZ4: 3.76 times faster in this fixture.
Stored payload grew from 1,071,662 to 1,259,404 bytes per table (+17.52%). Timing
excludes outer commit; sizes exclude relation overhead, WAL and backups. The
fixed-order samples do not establish a production speedup.

Full values are compared with independently calculated expectations, including
SQL NULL, JSON null, arrays/scalars, duplicate IDs, optional savepoint recovery,
required failure, outer rollback and explicit column overrides. The new helper
additionally requires native tests of the actual book boundary, stored codec,
session-setting restoration and real server-side unsupported-value recovery.

Production storage last reported 299,824,831 bytes (about 286 MiB) against its
existing 5 GB allocation, with the storage guard GREEN. This does not measure
future WAL growth, dead TOAST versions or vacuum pressure. The compute plans,
disk allocation and memory admission limits are unchanged.

## Release and acceptance

The candidate is based on main SHA `be798bc1f09f45688002b796798251946f69506d`,
including the parallel evidence-learning, structural latency and private-console
login repairs. The combined release is v91.7.25.
Exact final-head CI results are recorded in the pull request before release.
All mandatory workflows and native SQL cases must pass without hidden skips.

After deployment, verify the exact running SHA, actual compression diagnostic,
committed ordinary books, guard timings and current strategy-quality refresh.
Do not infer the 15-second target or memory recovery from a cold health check,
an isolated compression benchmark, or a cached HTTP 200 response. LZ4 does not
change existing pglz values until an ordinary write creates a new value.

Disabling the flag restores the prior setting for future transactions. It does
not recompress existing LZ4 values; PostgreSQL retains their codec and can read
both formats. No compute upgrade or broker action is part of this repair.

## PostgreSQL references

- [PostgreSQL 18: client connection defaults](https://www.postgresql.org/docs/18/runtime-config-client.html)
- [PostgreSQL 18: SET and savepoint scope](https://www.postgresql.org/docs/18/sql-set.html)
- [PostgreSQL 18: TOAST storage](https://www.postgresql.org/docs/18/storage-toast.html)
- [PostgreSQL 18: pg_attribute](https://www.postgresql.org/docs/18/catalog-pg-attribute.html)
- [PostgreSQL REL_18_STABLE GUC definitions](https://github.com/postgres/postgres/blob/REL_18_STABLE/src/backend/utils/misc/guc_tables.c)
