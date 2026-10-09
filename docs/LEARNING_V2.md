# VERITAS Learning 2.0 — implementation boundary

Added in this change:

- recurring false-block analysis from observed NO_TRADE outcomes;
- bounded shadow hypotheses for entry blocker relaxation;
- stop-geometry shadow candidates based on verified MAE/MFE;
- exit-capture shadow candidates based on realized capture ratio;
- regime-aware strategy-family router candidates;
- a dedicated learning-process entrypoint that has no broker import;
- explicit SHADOW_ONLY and no automatic production promotion.

Safety/causality boundary:

- a post-NO_TRADE move is not called a realizable missed profit;
- stop/exit alternatives are hypotheses, not reconstructed fills;
- real-money risk limits are never mutated;
- existing veritas_promotion.py remains the promotion gate;
- dedicated Render process must receive DATABASE_URL through Render secret
  management; credentials are not copied through source control.

Next integration step after independent validation:
move DB-facing read adapters from veritas_intelligence.py into a side-effect-free
package, then run veritas_learning_worker.py as its own Render service and remove
the corresponding heavy jobs from the trading process.


## Prospective registry (v91.8.27)

Learning 2.0 candidates now receive an immutable decision-ledger cutoff at
registration. Training evidence is frozen. Later scheduler runs only accumulate
decision IDs greater than that cutoff, so re-reading a recent window cannot
double-count evidence or turn training rows into validation rows.

Candidate states:
- COLLECTING / EVALUATING — prospective sample still building.
- SHADOW_ELIGIBLE — sufficient future evidence for a virtual experiment only.
- AWAIT_REPLAY — Stop/Exit hypotheses require ordered market-path replay and
  cannot be promoted from MAE/MFE summaries alone.
- REJECTED / EXPIRED — failed or stale evidence.

A shadow eligibility expires after seven days unless genuinely new evidence is
observed. A scheduler heartbeat cannot refresh validity. Production influence
remains false.


## Ordered Stop/Exit replay

Stop research now uses the same structural formulation as the canonical policy:
the stop remains beyond the same-timeframe swing anchor and only the ATR buffer
is varied in bounded shadow candidates: 0.10, 0.15 (current baseline), 0.20 and
0.30 ATR.

`veritas_learning_v2_replay.py` evaluates candidates only on time-ordered OHLC
bars from the declared source. A bar that touches both competing barriers is
`AMBIGUOUS_INTRABAR` and is excluded; OHLC data cannot reveal which level was
hit first. The same rule applies to the runner after a partial take-profit.

The replay module is a pure research utility. It has no broker or portfolio
imports, does not alter historical accounting, and has no production influence.
The registry keeps Stop/Exit candidates at `AWAIT_REPLAY` until a separate
history worker supplies an adequate ordered path.


## Runtime resource policy

The production shadow lane rotates one asset per run every 60 seconds. Each run
reads at most 192 materialized decision outcomes and 192 eligible trade episodes
for that asset. The outcome itself comes from `v90_decision_episodes`; the
ledger JSON is opened only to recover the frozen pre-outcome setup, source and
policy context. This replaces the previous all-market decision+outcome JSON join
that exceeded the five-second cooperative budget on the 512 MiB service.

The dedicated worker uses the same materialized evidence contract with a bounded
per-asset limit. Moving Learning 2.0 to that worker later therefore changes
where the research executes, not what evidence it is allowed to use.
