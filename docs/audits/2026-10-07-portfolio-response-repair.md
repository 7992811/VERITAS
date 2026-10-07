# Portfolio response repair — v91.7.18

## Observed production state

The repair is based on `b574a46` / v91.7.15, including the evening Brent source
pin, independent portfolio transactions, account-payload improvements and the
partial-response UI repair. Publication also integrates `0e4ace8` / v91.7.16,
preserving the projected post-funding protection reads and their CI checks.
It also preserves the subsequently merged owner structural-stop correction
(PR96) and Currency feed/retry recovery (PR97). This is a display/read-path repair.

Read-only checks on 7 October 2026:

| Observation | Result |
| --- | --- |
| Initial health request | HTTP 200, READY, v91.7.15; 12.225 seconds |
| Initial portfolio request | HTTP 200; 11.845 seconds; PARTIAL, stale_cache; five portfolios |
| Warm request completed at 19:53:13 UTC | HTTP 200; 7.980 seconds; PARTIAL, stale_cache; five portfolios, 23 positions; book observation age 31.561 seconds |
| Render service capacity | CPU limit 0.15; repeated measurements at that limit; memory limit 512 MiB |
| Brent position metadata | ProFinance, Brent oil / brent / 27; PINNED_PROVIDER_FEED |

These timings include network delay and server scheduling. They are not a
standalone measurement of SQL duration. The provider's instrument identifier 27
is not a delivery month or exchange contract. The existing Brent pin is retained.

## Confirmed defects and changes

1. The first portfolio caller executed the entire database read and enrichment
   synchronously. Concurrent callers were protected, but the owner could wait
   through several SQL queries. A single daemon worker now owns the refresh;
   the first HTTP caller waits up to one second for its result. A timeout does
   not create another reader. A pending response retains the original dates
   and explicitly marks the book and accounting as incomplete.
2. A 15-second browser polling interval could miss the 15-second observation
   freshness window, especially when the read itself exceeded that window.
   A genuinely completed, revision-matched read can now be delivered for
   30 seconds after publication with its original observation time and an
   explicit stale flag. One refresh runs concurrently. Completeness and
   freshness remain separate: the UI can apply a confirmed close as of that
   observation, displays the delay and rejects older observations.
3. A protective fill cleared the cache without advancing its revision. A read
   started before that fill could republish the former book. Protective changes
   now retire the live book and increment the revision using the established
   lock order. The cache writer and the HTTP delivery path both reject reads
   invalidated by a newer fill.
4. The API fetched the complete trade payload in its position join and fetched
   it again for account enrichment. Account enrichment now reuses the first
   payload from the same repeatable-read transaction. All original position
   and trade fields remain available for source, P&L and protection calculations.
5. The separate historical decision fallback transferred evidence graphs even
   though the display used only 11 decision fields and six trade-plan fields.
   SQL now projects those fields before decoding, preserving empty/nonempty
   objects, nulls and existing malformed-value behavior. This projection is not
   used for execution evidence.
6. A late background exception must not retain decoded rows through its
   traceback or chained cause. Those references are cleared before storing the
   error result. The log contains the exception type only.

## Verification

Required regressions cover a blocked first caller and simultaneous readers;
failure/retry; late exception memory release without cyclic GC; fill/read races;
SQL duration 2 seconds with a browser poll at 15.1 seconds; SQL duration
34 seconds with a poll at 45 seconds; original observation timestamps;
authoritative closes and browser-cache restoration; and parity of actual
accounting, source checks, protection calculations and rendered position HTML.

The new mandatory CI stage also runs the projection against isolated PostgreSQL
18, including a synthetic evidence graph larger than 2 MiB whose display
projection is below 1 KiB. Production data are not used as test fixtures.

The existing architecture ceilings and all existing required CI stages remain
in force. Server capacity and trading policies are unchanged. Production
latency must be measured again after deployment; this repair does not claim
that one second is an end-to-end network response guarantee.
