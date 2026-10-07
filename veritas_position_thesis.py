"""Position-bound structural exits, separate from permission to enter a signal.

This module does no I/O and never moves a stop. A fresh admission veto cannot
invalidate another event. Early exits need a causal closed-bar break on the
held timeframe and source; missing evidence leaves the independent stop intact.
"""
from copy import deepcopy
from datetime import datetime, timezone
import json

import veritas_price_source as VPS
import veritas_structural_breakout as SB
import veritas_timeframe_structure as TFS
from veritas_quote_time import quote_gate, utc_datetime

VERSION = "CTC_POSITION_THESIS_V1"
DECISION_KEY = "_position_thesis_decision"


def _payload(position):
    raw = (position or {}).get("payload") or {}
    try:
        return json.loads(raw) if isinstance(raw, str) else dict(raw)
    except (TypeError, ValueError):
        return {}


def entry_integrity_scope(direction, timeframe, reasons):
    """Keep all admission vetoes hard while stating their actual authority."""
    return {"invalidation_scope": "ENTRY_CANDIDATE", "candidate_direction": direction,
            "candidate_timeframe": timeframe, "entry_veto_reasons": list(reasons),
            "candidate_thesis_break_hint": "THESIS_INVALIDATION" in reasons,
            "exit_authority": "POSITION_BOUND_STRUCTURE_REQUIRED"}


def position_scope(position):
    p = _payload(position)
    context = p.get("timeframe_entry_context") or {}
    event = p.get("entry_event_snapshot") or context.get("event") or {}
    declared = p.get("execution_horizon") or p.get("execution_timeframe")
    timeframe = event.get("timeframe") or declared or p.get("entry_timeframe") or p.get("horizon")
    timeframe = timeframe or (position or {}).get("horizon")
    direction, asset = (position or {}).get("direction"), (position or {}).get("asset")
    identity = VPS.position_identity(dict(position or {}, payload=p))
    opened = TFS.timestamp((position or {}).get("opened_at") or p.get("entry_at"))
    errors = []
    if direction not in ("LONG", "SHORT") or timeframe not in TFS.TIMEFRAMES:
        errors.append("HELD_DIRECTION_OR_TIMEFRAME_MISSING")
    if not identity or opened is None:
        errors.append("HELD_SOURCE_OR_ENTRY_TIME_MISSING")
    if event.get("direction") and event["direction"] != direction:
        errors.append("HELD_EVENT_DIRECTION_CHANGED")
    if event.get("asset") and event["asset"] != asset:
        errors.append("HELD_EVENT_ASSET_CHANGED")
    if event.get("timeframe") and any(h and h != timeframe for h in
            (declared, p.get("execution_timeframe"))):
        errors.append("HELD_EVENT_TIMEFRAME_CHANGED")
    if event.get("source_identity") and not VPS.same(identity, event["source_identity"]):
        errors.append("HELD_EVENT_SOURCE_CHANGED")
    quote_structure = (event.get("version") == SB.VERSION or
                       p.get("structural_policy_version") == SB.DEFAULT_POLICY["version"])
    management = timeframe
    anchor, revision_at = VPS.positive(event.get("stop_anchor")), None
    if quote_structure:
        management = event.get("structural_timeframe")
        if not SB.validate_event(event, identity)["eligible"]:
            errors.append("HELD_QUOTE_EVENT_PROOF_INVALID")
        if (management not in TFS.TIMEFRAMES or any(event.get(k) != management
                for k in ("stop_timeframe", "atr_timeframe"))
                or any(p.get(k) and p[k] != management for k in ("management_horizon", "structural_timeframe"))):
            errors.append("HELD_PARENT_TIMEFRAME_CHANGED")
        signal = TFS.timestamp(event.get("signal_at"))
        if signal is None or opened is None or signal > opened:
            errors.append("HELD_QUOTE_EVENT_POSTDATES_ENTRY")
        revision = p.get("same_tf_trailing") or {}
        if revision.get("management_rule") == "PROTECTED_PARENT_SWING":
            checked = SB.validate_management_revision(revision, event, opened)
            if checked["eligible"]:
                anchor, revision_at = checked["anchor"], checked["quote_observed_at"]
            else:
                errors.append("HELD_PARENT_REVISION_PROOF_INVALID")
    return {"position_id": (position or {}).get("active_trade_id"), "asset": asset,
            "held_direction": direction, "execution_timeframe": timeframe,
            "management_timeframe": management, "quote_structural_binding": quote_structure,
            "entry_event_id": event.get("event_id") or p.get("entry_event_id") or p.get("r66_event_id") or p.get("signal_event_id"),
            "source_identity": deepcopy(identity), "opened_at": opened,
            "invalidation_level": anchor, "protected_revision_at": revision_at,
            "legacy_binding": not bool(event), "binding_valid": not errors,
            "binding_errors": errors}


def _same_source(expected, actual):
    return VPS.same(expected, actual) and VPS.same(actual, expected)


def _evaluate_quote_structure(position, row, clock, scope, out):
    """A fast trigger does not turn the parent risk thesis into a fast exit."""
    asset, identity, parent = scope["asset"], scope["source_identity"], scope["management_timeframe"]
    if not row or row.get("asset") != asset:
        return dict(out, reason="HELD_PARENT_CONTEXT_MISSING")
    if not _same_source(identity, VPS.identity(asset, row)):
        return dict(out, reason="HELD_SOURCE_CONTEXT_MISMATCH")
    q = VPS.quote_from_row(row)
    observed, px = TFS.timestamp(q.get("observed_at")), VPS.positive(q.get("price"))
    if (q.get("source_gate_pass") is not True or q.get("market_open") is not True
            or not px or not VPS.matches(position, q)
            or not quote_gate(q.get("observed_at"), now=clock, execution=True, asset=asset)["eligible"]
            or observed is None or not scope["opened_at"] <= observed <= clock.timestamp()
            or (scope.get("protected_revision_at") or 0) > observed):
        return dict(out, reason="HELD_SOURCE_FRESH_QUOTE_REQUIRED")
    ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
    if (ctx.get("version") != SB.VERSION or ctx.get("structural_timeframe") != parent
            or ctx.get("timeframe") != row.get("horizon")
            or not _same_source(identity, ctx.get("source_identity"))):
        return dict(out, reason="HELD_PARENT_CONTEXT_MISSING")
    ctx = SB.rebind_quote(ctx, q, clock)
    check = SB.context_gate(ctx, clock)
    if not check["eligible"]:
        return dict(out, reason="HELD_PARENT_CONTEXT_INVALID", context_gate=check)
    event = ctx.get("event") or {}
    opposite = "SHORT" if scope["held_direction"] == "LONG" else "LONG"
    if event.get("direction") != opposite:
        return dict(out, reason="NO_OPPOSITE_HELD_PARENT_BREAK")
    if (event.get("trigger_timeframe") != parent or event.get("structural_timeframe") != parent
            or event.get("asset") != asset or event.get("event_id") == scope["entry_event_id"]
            or not SB.validate_event(event, identity)["eligible"]):
        return dict(out, reason="HELD_PARENT_BREAK_PROVENANCE_MISMATCH")
    signal = TFS.timestamp(event.get("signal_at"))
    if signal is None or not scope["opened_at"] < signal <= observed:
        return dict(out, reason="HELD_PARENT_BREAK_NOT_AFTER_ENTRY")
    if event.get("spent") and event.get("spent_reason") != "STRUCTURAL_TARGET_ALREADY_REACHED":
        return dict(out, reason="HELD_OPPOSITE_BREAK_REVERSED")
    sign = 1 if opposite == "LONG" else -1
    trigger, anchor = VPS.positive(event.get("trigger_level")), scope["invalidation_level"]
    if not trigger or sign * (px - trigger) <= 0:
        return dict(out, reason="HELD_PARENT_BREAK_NOT_HELD")
    if not anchor or sign * (px - anchor) <= 0:
        return dict(out, reason="HELD_PROTECTED_PARENT_INTACT")
    return dict(out, exit_authorized=True, reason="HELD_PROTECTED_PARENT_INVALIDATED",
                structure_confirmed=True,
                proof={"event_id": event["event_id"], "entry_event_id": scope["entry_event_id"],
                       "direction": opposite, "timeframe": parent,
                       "execution_timeframe": scope["execution_timeframe"],
                       "confirmation": "VERIFIED_QUOTE_PARENT_BREAK",
                       "source_identity": deepcopy(identity), "trigger_level": trigger,
                       "invalidation_level": anchor, "signal_price": event["signal_price"],
                       "quote_price": px, "signal_at": signal,
                       "quote_observed_at": observed, "event_proof_hash": event["proof_hash"],
                       "level_available_at": event["level_available_at"],
                       "atr_observed_until": event["atr_observed_until"]})


def _evaluate(position, row, now):
    scope = position_scope(position)
    clock = utc_datetime(now) if now is not None else datetime.now(timezone.utc)
    plan = (row or {}).get("trade_plan") or {}
    ti = plan.get("trade_integrity") or {}
    reasons = list(ti.get("hard_reasons") or ti.get("ctc_original_hard_reasons") or [])
    out = {"version": VERSION, "exit_authorized": False, "held": scope,
           "evaluated_at": clock.isoformat() if clock else None,
           "candidate_direction": (row or {}).get("research_decision"),
           "candidate_hard_reasons": reasons,
           "candidate_veto_is_exit_authority": False,
           "reason": "POSITION_BINDING_INCOMPLETE"}
    if not scope["binding_valid"] or clock is None:
        return out
    if scope["quote_structural_binding"]:
        return _evaluate_quote_structure(position, row, clock, scope, out)
    horizon, asset, identity = scope["execution_timeframe"], scope["asset"], scope["source_identity"]
    if not row or row.get("asset") != asset or row.get("horizon") != horizon:
        return dict(out, reason="HELD_TIMEFRAME_CONTEXT_MISSING")
    if not VPS.same(identity, VPS.identity(asset, row)):
        return dict(out, reason="HELD_SOURCE_CONTEXT_MISMATCH")
    q = VPS.quote_from_row(row)
    observed = TFS.timestamp(q.get("observed_at"))
    px = VPS.positive(q.get("price"))
    if (not q.get("source_gate_pass") or not px or not VPS.matches(position, q)
            or not quote_gate(q.get("observed_at"), now=clock, protective=True)["eligible"]
            or observed is None or observed < scope["opened_at"]):
        return dict(out, reason="HELD_SOURCE_FRESH_QUOTE_REQUIRED")
    ctx = row.get("timeframe_entry_context") or plan.get("timeframe_entry_context") or {}
    closed = TFS.timestamp(ctx.get("closed_at"))
    seconds = TFS.TIMEFRAMES[horizon]
    if (ctx.get("version") != TFS.VERSION or ctx.get("status") != "OK"
            or ctx.get("timeframe") != horizon or ctx.get("atr_timeframe") != horizon
            or not _same_source(identity, ctx.get("source_identity"))
            or closed is None or closed > min(clock.timestamp(), observed)
            or clock.timestamp() - closed > seconds):
        return dict(out, reason="HELD_CLOSED_TIMEFRAME_STRUCTURE_REQUIRED")
    event = ctx.get("event") or {}
    opposite = "SHORT" if scope["held_direction"] == "LONG" else "LONG"
    if event.get("direction") != opposite:
        return dict(out, reason="NO_OPPOSITE_HELD_TIMEFRAME_BREAK")
    if (event.get("version") != TFS.VERSION or not event.get("event_id")
            or event.get("event_id") == scope["entry_event_id"]
            or event.get("asset") != asset
            or event.get("event_type") != "SAME_TIMEFRAME_STRUCTURAL_BREAKOUT"
            or any(event.get(k) != horizon for k in ("timeframe", "atr_timeframe", "stop_timeframe", "target_timeframe"))
            or event.get("confirmation") != "CLOSED_" + horizon + "_BAR"
            or not _same_source(identity, event.get("source_identity"))):
        return dict(out, reason="HELD_BREAK_PROVENANCE_MISMATCH")
    opening, signal, confirmed = (TFS.timestamp(event.get(k)) for k in
                                  ("breakout_bar_at", "signal_at", "confirmed_at"))
    known = [TFS.timestamp(event.get(k)) for k in
             ("level_available_at", "stop_level_available_at", "atr_observed_until")]
    if (opening is None or signal is None or confirmed is None
            or signal != opening + seconds or not scope["opened_at"] < signal <= confirmed <= closed
            or any(t is None or t > opening for t in known)
            or clock.timestamp() - confirmed > seconds):
        return dict(out, reason="HELD_BREAK_NOT_CAUSAL_OR_CURRENT")
    # An event invalidated by its own stop is no longer proof of a live reversal.
    # Reaching the new side's target does not restore the old held thesis.
    if event.get("spent") and event.get("spent_reason") != "SAME_TF_TARGET_ALREADY_REACHED":
        return dict(out, reason="HELD_OPPOSITE_BREAK_REVERSED")
    sign = 1 if opposite == "LONG" else -1
    trigger, close = VPS.positive(event.get("trigger_level")), VPS.positive(event.get("signal_price"))
    if not trigger or not close or sign * (close - trigger) <= 0 or sign * (px - trigger) <= 0:
        return dict(out, reason="HELD_BREAK_CLOSE_AND_QUOTE_REQUIRED")
    anchor = scope["invalidation_level"]
    if not scope["legacy_binding"] and not anchor:
        return dict(out, reason="HELD_EVENT_INVALIDATION_LEVEL_MISSING")
    if anchor and (sign * (close - anchor) <= 0 or sign * (px - anchor) <= 0):
        return dict(out, reason="HELD_EVENT_INVALIDATION_LEVEL_INTACT")
    out["structure_confirmed"] = True
    guard = plan.get("ctc_open_position_thesis_guard") or {}
    if seconds >= TFS.TIMEFRAMES["1h"] and not guard.get("hard_exit_allowed"):
        return dict(out, reason="HELD_SENIOR_CONFIRMATION_REQUIRED")
    return dict(out, exit_authorized=True,
                reason="LEGACY_SAME_TF_STRUCTURAL_REVERSAL" if scope["legacy_binding"] else "HELD_EVENT_STRUCTURE_INVALIDATED",
                proof={"event_id": event["event_id"], "direction": opposite, "timeframe": horizon,
                       "source_identity": deepcopy(identity), "trigger_level": trigger,
                       "invalidation_level": anchor, "closed_price": close, "quote_price": px,
                       "signal_at": signal, "confirmed_at": confirmed, "closed_at": closed,
                       "quote_observed_at": q.get("observed_at"),
                       "level_available_at": event["level_available_at"],
                       "atr_observed_until": event["atr_observed_until"],
                       "senior_confirmation": deepcopy(guard) if guard else None})


def evaluate_exit(position, row, now=None):
    """Malformed diagnostics must not interrupt the separate STOP/risk path."""
    try:
        return _evaluate(position, row, now)
    except Exception as exc:
        return {"version": VERSION, "exit_authorized": False,
                "reason": "THESIS_DIAGNOSTICS_UNAVAILABLE", "error_type": type(exc).__name__}


def management_row(summary, position, now=None):
    """Select only the held source/timeframe; do not select by veto or confidence."""
    try:
        scope = position_scope(position)
        def held_context(row):
            if not scope["quote_structural_binding"]:
                return row.get("horizon") == scope["execution_timeframe"]
            ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
            return (ctx.get("version") == SB.VERSION and
                    ctx.get("structural_timeframe") == scope["management_timeframe"] and
                    ctx.get("timeframe") == row.get("horizon"))
        rows = [r for r in summary or [] if r.get("asset") == scope["asset"]
                and held_context(r)
                and VPS.same(scope["source_identity"], VPS.identity(scope["asset"], r))]
        if not rows:
            return None
        def freshness(row):
            ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
            if scope["quote_structural_binding"]:
                # The parent break observed in a fast lane remains parent
                # evidence. An unrelated micro event cannot displace it.
                event = ctx.get("event") or {}
                opposite = "SHORT" if scope["held_direction"] == "LONG" else "LONG"
                return (bool(event.get("trigger_timeframe") == scope["management_timeframe"]
                             and event.get("direction") == opposite
                             and SB.validate_event(event, scope["source_identity"])["eligible"]),
                        TFS.timestamp(VPS.quote_from_row(row).get("observed_at")) or 0,
                        TFS.timestamp(ctx.get("structure_closed_at")) or 0)
            return (TFS.timestamp(ctx.get("closed_at")) or 0,
                    TFS.timestamp(VPS.quote_from_row(row).get("observed_at")) or 0)
        result = deepcopy(max(rows, key=freshness))
        result[DECISION_KEY] = evaluate_exit(position, result, now)
        return result
    except Exception:
        return None


def hard_thesis_exit(row):
    decision = (row or {}).get(DECISION_KEY) or {}
    return bool(decision.get("version") == VERSION and decision.get("exit_authorized")
                and (decision.get("held") or {}).get("binding_valid") and decision.get("proof"))


def journal_position(position, row, reason, now=None):
    """Attach a reviewable decision to the close snapshot without mutating entry."""
    try:
        result = dict(position or {})
        p = _payload(result)
        decision = deepcopy((row or {}).get(DECISION_KEY) or evaluate_exit(position, row, now))
        decision["exit_reason"] = str(reason)
        decision["protective_exit_independent"] = str(reason) in ("STOP", "RISK_HARD_STOP")
        p["last_exit_thesis_decision"] = decision
        result["payload"] = p
        return result
    except Exception:
        return position
