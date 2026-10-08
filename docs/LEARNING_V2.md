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
