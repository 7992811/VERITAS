# R79 — Signal-authoritative staged entry

Date: 2026-10-05

## User requirement

If VERITAS itself publishes a directional LONG/SHORT signal, the paper portfolio must start a staged position. An older parent breakout being stale, already extended, or having reached its original target must not leave the portfolio flat while the current signal is still active.

## Policy

- A current displayed directional signal is rebased into a new `R79_SIG_*` setup identity.
- Parent event age/extension remains audit metadata but is not the execution trigger.
- Current signal direction must agree with the selected execution direction.
- Old soft vetoes such as WAIT_RETEST, EVENT_TARGET_REACHED, RR floor, and a small expected-move/cost-buffer miss no longer flatten a current published signal when post-cost reward remains positive.
- Hard controls remain: valid source/session, fresh execution quote, valid stop, no hard invalidation, no fast-TF direction conflict, no negative validated edge, and stop-risk cap.
- Existing-position adds still require the normal distinct confirmation path.

## Initial paper sizing

- Aggressive: 50% normal signal, up to 100% SUPER signal, subject to risk cap.
- Impulse: 20% normal, 40% SUPER.
- Champion/Core: 10% normal, 25% SUPER.
- Challenger: 10% normal, 25% SUPER.

## Validation

Dedicated R79 targeted CI passed:
- stale parent event -> fresh R79_SIG setup;
- normal directional signal -> Aggressive admission open=true, >=50%;
- SUPER signal -> larger staged start subject to risk cap;
- full open path bypasses only the old soft-veto chain and reaches the order mutation layer.

Production live-trading authorization remains unchanged; this release changes the paper/execution-candidate entry policy.
