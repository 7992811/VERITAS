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

The production shadow lane rotates one asset per run on a 120-second base cadence. Each run
reads at most 96 materialized decision outcomes and 96 eligible trade episodes
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


## Event-driven cadence and path evidence (v91.8.30)

The production web process is CPU-limited. Learning 2.0 therefore uses a
96-row asset cohort with a six-second atomic budget and a 120-second base
interval. Stop/Exit replay uses a 600-second idle interval, but generation of a
new Stop/Exit hypothesis immediately requests the existing coalesced replay job.
This avoids repeated NO_WORK polling and avoids duplicate asset work after a
missed checkpoint.

Missed-entry discovery now uses the largest observed move in the frozen signal
direction: terminal return or the decision episode's MFE/MAE path excursion.
For LONG, favorable path evidence is MFE; for SHORT, it is -MAE. This is evidence
that a directional move was observed, not proof that a hypothetical order would
have survived its stop or captured that return.

Prospective promotion remains stricter: path excursion can count as a favorable
observation, but the candidate still requires positive average terminal signed
return. Stop ordering and executable counterfactual P&L remain the responsibility
of the separate ordered-path replay layer.

Additional timing codes such as R66_WAIT_RETEST and
STRUCTURAL_ENTRY_TOO_LATE_TO_TARGET are observable for diagnosis only. They are
not added to the learnable relaxation whitelist.
