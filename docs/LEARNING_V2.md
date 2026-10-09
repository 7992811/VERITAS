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
double-count evidence or turn training rows into validation rows. Cohorts also
freeze source key, exact contract identity and policy hash; evidence from another
expiry, provider or rule version cannot validate the candidate.

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
reads at most 128 materialized decision outcomes and 128 eligible trade episodes
for that asset. The outcome itself comes from `v90_decision_episodes`; the
ledger JSON is opened only to recover the frozen pre-outcome setup, source and
policy context. This replaces the previous all-market decision+outcome JSON join
that exceeded the five-second cooperative budget on the 512 MiB service.

The dedicated worker uses the same materialized evidence contract with a bounded
per-asset limit. Moving Learning 2.0 to that worker later therefore changes
where the research executes, not what evidence it is allowed to use.


## Prospective Stop/Exit evaluation (v91.8.28)

Stop and Exit candidates are evaluated only on eligible paper trades closed
after the candidate was registered. The evaluator reads already cached canonical
bars; it never downloads history from the learning lane.

Replay constraints:

- price source and exact contract must match the frozen candidate cohort;
- policy hash, asset, regime and horizon must match;
- only one-entry trades are admitted to this first replay protocol;
- holding time is capped at 24 hours so the replay does not omit funding;
- entry-bar high/low is discarded when the fill occurred inside that bar;
- bars after the actual trade close are discarded even if present in cache;
- a same-bar stop/target conflict is `AMBIGUOUS_INTRABAR`, never resolved in
  the candidate's favor;
- commission plus modeled slippage use the canonical cost policy;
- the replay baseline parameter must match the frozen entry event policy
  (stop buffer or first-target fraction); policy drift invalidates the episode.

A Stop/Exit candidate becomes `REPLAY_SUPPORTED` only after at least 32
comparable future trades across seven UTC days, positive candidate net return,
positive improvement over the baseline with a positive paired 95% lower confidence bound, and ambiguity no greater than 15%.
Persistent non-positive improvement at 64 observations across 14 days rejects
the candidate. `REPLAY_SUPPORTED` is research evidence only: it is not a
shadow champion and has no production authority.


## Blocked-decision evidence (v91.8.29)

A directional market view and an executable entry are separate facts. Learning
2.0 now treats a directional LONG/SHORT decision as a missed-entry candidate
when admission was actually blocked and the later verified move was favorable.

Block proof is fail-closed. A block exists only when at least one of these is
recorded at decision time:

- structured final-gate blockers are non-empty;
- plan/trade admission is explicitly false;
- final gate status is BLOCK.

Reason strings never manufacture a block. Once a block is proven, declared
reason fields (`plan_reason`, `trade_entry_reason`, execution reasons) are
parsed only for tokens already present in the canonical blocker catalog.
Arbitrary prose cannot become a new policy.

Hard-veto blockers remain non-learnable. Only the declared timing/retest/expiry
blockers can create an Entry relaxation hypothesis. The runtime also publishes
bounded diagnostics explaining a zero-candidate result: cohort size, blocked
directional count, favorable missed moves, learnable/hard-veto counts,
unparsed blocked episodes and the maximum recurring learnable blocker count.
Raw reason prose is not emitted.


## Admission evidence protocol (v91.8.30)

Learning 2.0 no longer assumes that entry blockers live at one JSON level.

The canonical decision compactor now persists a small, immutable admission
snapshot alongside the existing nested trade-plan and execution-eligibility
objects:

- `plan_eligible` and `plan_reason`;
- `trade_entry_eligible` and `trade_entry_reason`;
- `paper_execution_reason`;
- `final_gate_status`;
- bounded structured `final_gate_blockers`.

This is a duplicate of facts already present in the decision graph and changes
no signal, order, risk limit or execution behavior.

For backward compatibility, the Learning 2.0 reader also accepts legacy compact
decisions where the same evidence exists only under
`trade_plan.*` or `execution_eligibility.*`. Existing historical ledger rows
therefore remain usable without rewriting history.

A free-text reason can explain a proven block only if it contains a blocker
already present in the canonical blocker catalog. Reason text by itself never
creates a block or a new policy.


## Admission-state persistence (v91.8.31)

Decision deduplication treats execution admission as material state.

The durable decision signature now includes:

- execution eligibility and reason;
- paper eligibility and paper execution reason;
- the sorted structured paper-source blocker set.

Therefore a signal can remain LONG/SHORT with the same structural event while a
new execution/admission state still produces a new compact decision event. This
prevents an earlier admitted or unblocked snapshot from hiding a later blocked
state inside the same signal bar.

The signature change affects only durable event persistence. It does not alter
the signal, order, risk, execution or broker decision itself.


## Outcome evidence tier (v91.8.32)

Closed-trade evidence is split into two independent authority levels.

**Outcome evidence** requires the original event/source, immutable first fill,
initial stop and ATR, complete cash accounting, and a valid closed-trade clock.
It may train only net-outcome, position-size and probability/calibration logic.

**Path evidence** additionally requires the continuous observation-path witness.
Only this tier may support MFE/MAE, capture, stop-quality, exit-quality or
strategy-quality conclusions.

A trade with an observation gap is therefore VERIFIED_OUTCOME_ONLY rather
than wholly discarded. Its learning_eligible flag remains false, its
strategy_quality_eligible flag remains false, and it cannot enter path-based
profiles. A separate outcome_learning_eligible stamp plus frozen evidence hash
allows the closed-trade microlearner to consume the proven net cash outcome.

Materialization is backward-compatible: existing episodes missing the outcome
evidence version/hash are reprocessed from their stored immutable evidence.
No historical price path is reconstructed and no missing prospective admission
stamp is invented.


## Outcome-tier Stop/Exit research (v91.8.33)

The evidence tiers are now explicit:

1. **Verified net outcome** may seed a bounded Stop or Exit replay experiment.
2. **Continuous path evidence** remains required for direct MFE/MAE/capture
   statistics.
3. **Ordered exact-source replay** is the only mechanism that can support a
   Stop/Exit counterfactual candidate.

This prevents an observation-path gap from silencing Stop/Exit research while
still refusing to infer MFE/MAE from incomplete quote sampling.

Research trade cohorts use
`outcome_learning_eligible=true`, current diagnostics provenance, immutable
source/contract/policy identity and a replay-geometry check. They are deduplicated
by `independent_episode_key`, so the same market idea represented in several
portfolios counts once.

Stop challengers are 0.10, 0.20 and 0.30 ATR around the recorded structural
anchor; 0.15 ATR is the baseline and is no longer emitted as its own Challenger.

Exit challengers remain explicit 25% and 75% first-target fractions versus the
50% baseline and require a frozen runner target.

Replay is also fail-closed on the partial entry candle: if that candle touches
any competing stop/target barrier, intrabar order is unknowable and the episode
is marked ambiguous rather than favorable.

No replay candidate has production authority. Existing prospective replay sample
and confidence-interval gates remain unchanged.


## Observation path V2 (v91.8.33)

The paper-path witness now separates two clocks that were previously conflated:

- **protective check continuity** remains strict: checks are expected every 15
  seconds and any gap above 45 seconds invalidates path evidence;
- **provider timestamp cadence** is source-execution bounded: BTC/ETH quotes may
  be at most 30 seconds old, other execution quotes at most 120 seconds old.
  A new provider timestamp may arrive one protective cycle after that ceiling,
  so provider gaps are bounded at 45 seconds for BTC/ETH and 135 seconds for
  other assets.

This does not convert sampled quotes into tick-complete history. MFE/MAE remain
explicitly sampled-source evidence. A service restart, missing protective checks,
source/contract mismatch, stale processing, post-exit observation or malformed
witness still fails closed.

The change only affects evidence quality for future trades. Existing V1 paths are
not upgraded or backfilled.


## Seeded-only observation sampling (v91.8.36)

The observation sidecar keeps the health/cleanup model introduced in v91.8.35
but no longer spends evidence writes on positions that can never become
path-eligible.

Rules:

- a sidecar witness seeded at the exact entry is sampled normally;
- an already-observed canonical prefix may be handed off only when it started at
  the real entry and has no prior invalid observation or path gap;
- a carried position with no entry seed is counted but not rewritten;
- a seeded witness that already proved an invalid observation or path gap is
  counted as irrecoverable and is not resampled;
- a transient missing cached quote produces **no evidence write**. If the outage
  exceeds the allowed cadence, the next real observation proves the gap through
  the existing observation-path rules.

Telemetry now separates seeded, handed-off, unseeded, irrecoverable and actually
sampled positions. The sidecar remains cached-only, uses no network fetches,
takes no paper-book lock and has no trading authority.


## Closed-trade backlog scheduling (v91.8.42)

Historical outcome-tier rematerialization uses the existing durable four-phase
closed-trade learner. The cooperative trade batch is reduced from four to two
trades so one SQL/diagnostic phase remains inside the existing six-second
background budget.

When a bounded phase actually advances backlog state, it coalesces another
`learning_trade_evidence` request with a five-second retry interval. The
budget is not widened and no parallel worker is created. A materialize pass with
zero rows stops the self-request chain and returns to the normal 30-second
periodic cadence.

A successful materialize pass also coalesces `learning_v2_shadow` so newly
proved outcome-tier rows can become research evidence without waiting for the
next scheduled Learning 2.0 rotation.
