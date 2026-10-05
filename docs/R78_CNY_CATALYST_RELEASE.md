# R78 — CNYRUBF catalyst continuation

Date: 2026-10-05

## Problem

CNYRUBF had a fresh 5m SUPER_LONG / BUY state after the Ministry of Finance announced a sharp increase in FX/gold purchases, but paper admission stayed attached to an old R69 breakout event (trigger 12.559, 46 bars old). The anti-chase gate therefore measured the current quote against the stale event and returned R66_WAIT_RETEST / R74_EVENT_TARGET_REACHED.

## Fix

- verified fundamental catalyst can create a NEW CATALYST_CONTINUATION setup identity;
- the old breakout remains the parent event but no longer determines current extension;
- fresh catalyst continuation is rebased to the current quote and current structural stop;
- modeled adverse fill impact is no longer mistaken for market extension in the catalyst timing gate;
- if the only remaining economics veto is the fixed net-R/R floor, a high-quality verified catalyst may open a small paper probe rather than a full allocation;
- probe requires SUPER/strong structure, >=5 independent evidence families, positive post-cost reward, fresh quote/source, no hard invalidation and no negative historical-edge veto;
- Aggressive probe is capped at 25%; other modes remain smaller;
- further size is added only after a distinct structural confirmation through the existing scale engine;
- catalyst exception is time-bounded; normal anti-chase rules resume after expiry.

## Validation

Dedicated CNY targeted CI:
- catalyst rebase/expiry tests: PASS;
- full CNY portfolio admission diagnostic: open=true, fraction=0.25, reason=R78_CATALYST_CONTINUATION_PROBE;
- new trend event: R69_CAT_*, extension_atr=0.0, R66_EVENT_READY.

The main Safety CI still has the same pre-existing failures already present on R77; the CNY patch does not add new failures.
