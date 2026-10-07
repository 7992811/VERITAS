# Persistent evidence learning

`EVIDENCE_LEARNING_LOOP_V2_MOSCOW_HOURS` connects the evidence-learning protocol to the existing
VERITAS paper-learning callbacks, PostgreSQL ledger and background history permit.
It replaces the in-memory heavy-learning timer, not the canonical trading engine.

One stage runs at a time: completed decision episodes → event outcomes → rule
statistics → execution lessons → verified decision memory → evidence manifest →
historical quality diagnostics. A stage releases its proof objects and trims
memory before the next stage. Admission uses the existing 260 MB learning limit
and shared history permit. It never acquires a paper-book lock itself.

Full cycles start daily on whole Moscow hours: 07:00, 08:00, ..., 23:00
(17 slots, including weekends; Europe/Moscow, UTC+03:00). The stage admission
window closes at 23:50. There is no extra 23:50 cycle. A stage already executing
may finish and checkpoint safely; subsequent stages and retries resume at 07:00.
Late or missed slots coalesce into one run, never a burst of historical runs.
Completing a run schedules the next whole-hour slot, not an hour after completion.

The initial due time is the first slot after the configured startup delay
(`VERITAS_HEAVY_LEARNING_START_DELAY_SECONDS`, existing minimum 300 seconds).
Restarts keep that due time and resume after the last committed stage. The
calendar supersedes the old interval-only heavy-learning setting. It applies to
this full evidence cycle; continuous quote/outcome capture keeps its own timing.
The worker checks every 15 seconds and requests from the market cycle only wake
that worker. There is no additional paid service or keepalive. Free-host sleeping
still suspends all work; `always_on_confirmed` reports the existing configuration.

The session advisory lock prevents simultaneous stages in overlapping deploys.
An interrupted stage is replayed through its existing idempotent upserts; this is
at-least-once recovery, not a promise of exactly-once execution. Three reported
failures exhaust a stage; later stages can run, but the whole run is `DEGRADED`.
Resource deferrals do not consume attempts. A release/protocol change supersedes
the old run with an audit record instead of mixing implementation versions.
Migrating from V1 creates a V2 checkpoint on the Moscow calendar; old audit rows
remain intact. The status API exposes `schedule`, including timezone, opening,
last start, stage cutoff and current window availability, plus `next_due_at`.

State lives at `ledger_events.event_key=evidence_learning_state:canonical`.
Append-only `evidence_learning_audit` events record scheduling, stage attempts,
results, completion and supersession in the same transaction as the checkpoint.
There is no new schema migration or portfolio-data rewrite. Payloads are limited
to 64 KB; raw proof graphs, broker credentials and exception text are excluded.

`/api/v1/evidence-learning` and `/api/v1/heavy-learning` expose the same cached,
nonblocking status. After restart the worker restores it from PostgreSQL. The
evidence stage reuses `veritas_learning_exports.decision_lessons`, including its
current source, path, duplicate and raw-member hash checks. It records at most 64
recent verified lesson IDs plus a manifest digest. This is a bounded window,
not a total count of all knowledge or independently sampled market events.

Counts from canonical callbacks describe processed observations/upsert attempts;
they are explicitly not advertised as newly learned independent knowledge.
Quality snapshots carry their original calculation time and historical scope.
A change in these diagnostics is not a prospective controlled test of the new
pipeline, and cannot automatically approve a new strategy. Model weights are
not trained. Existing rule lifecycle and verified paper-memory policies remain
the authority for their own outputs.

Validation: `python -m unittest -v test_veritas_evidence_learning.py`; PostgreSQL
exclusion, lock release and checkpoint/audit rollback cases additionally require
`VERITAS_QUALITY_TEST_DSN` pointing at the isolated `veritas_quality_test` database.
The required Safety CI runs those native database cases with all existing gates.

Rollback: revert the integration and module commit, redeploy, verify the previous
heavy-learning status endpoint and release SHA. Preserve the new ledger events;
the previous runtime ignores their event types. No historical evidence or paper
financial records need to be deleted.
