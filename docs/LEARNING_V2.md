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
