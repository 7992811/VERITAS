"""Same-timeframe structural management for explicitly tagged new positions.

Legacy positions keep their existing policy. A new closed, confirmed swing on
the entry timeframe can reduce risk; no quote-only or lower-timeframe stop can.
"""
import json
from copy import deepcopy

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_profit_protection as VPP
import veritas_position_thesis as PT
import veritas_structural_breakout as SB
import veritas_timeframe_structure as TFS
from veritas_quote_time import quote_gate, utc_datetime

VERSION = "CTC_SAME_TF_MANAGEMENT_V2"

# Legacy-only readers may omit this proven subset of owns_position before
# transferring full position evidence. Other JSON shapes and non-string tags
# still reach the existing Python ownership/validation path unchanged.
LEGACY_POSITION_SQL_PREDICATE = """NOT COALESCE(jsonb_path_exists(payload,
    'strict $.structural_policy_version ? (@.type() == "string" && @ != "")',
    '{}'::jsonb, true), false)"""


def payload(position):
    value = (position or {}).get("payload") or {}
    try:
        return json.loads(value) if isinstance(value, str) else dict(value)
    except (TypeError, ValueError):
        return {}


def owns_position(position):
    return bool(payload(position).get("structural_policy_version"))


def owns_row(row):
    return bool((row or {}).get("structural_policy_version") or
                (row or {}).get("timeframe_entry_context") or
                ((row or {}).get("trade_plan") or {}).get("timeframe_entry_context"))


def filter_lower_context(position, candidates, summary):
    """A lower-frame signal cannot become this tagged position's exit thesis."""
    book, rows = dict(candidates or {}), list(summary or [])
    if not owns_position(position):
        return book, rows
    scope = PT.position_scope(position)
    asset = position.get("asset")
    horizon = (scope["management_timeframe"] if scope["quote_structural_binding"]
               else payload(position).get("execution_horizon"))
    seconds = TFS.TIMEFRAMES.get(horizon)
    if seconds is None:
        book.pop(asset, None)
        return book, [r for r in rows if r.get("asset") != asset]
    def usable(row):
        if scope["quote_structural_binding"] and row.get("asset") == asset:
            if not scope["binding_valid"]:
                return False
            ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
            event = ctx.get("event") or {}
            # A parent break may first be observed by the 1m quote lane. Its
            # explicit trigger TF, not the display lane, establishes authority.
            return bool(ctx.get("version") == SB.VERSION and ctx.get("structural_timeframe") == horizon
                        and event.get("trigger_timeframe") == horizon
                        and SB.validate_event(event, scope["source_identity"])["eligible"])
        return row.get("asset") != asset or TFS.TIMEFRAMES.get(row.get("horizon"), 0) >= seconds
    if asset in book and not usable(book[asset]):
        book.pop(asset)
    return book, [r for r in rows if usable(r)]


def _parent_trailing_candidate(position, summary, quote, now, scope):
    p = payload(position)
    horizon, asset = scope["management_timeframe"], scope["asset"]
    out = {"version": VERSION, "eligible": False, "reason": "STRUCTURAL_PARENT_TRAIL_CONTEXT_REQUIRED",
           "timeframe": horizon, "management_rule": "PROTECTED_PARENT_SWING"}
    clock = utc_datetime(now)
    quote = quote or {}
    if not scope["binding_valid"] or clock is None:
        return dict(out, reason="STRUCTURAL_PARENT_POSITION_BINDING_INVALID")
    if (quote.get("source_gate_pass") is not True or quote.get("market_open") is not True
            or not VPS.matches(position, quote)
            or not quote_gate(quote.get("observed_at"), now=clock, execution=True, asset=asset)["eligible"]):
        return dict(out, reason="STRUCTURAL_PARENT_FRESH_SOURCE_QUOTE_REQUIRED")
    identity = scope["source_identity"]
    px, entry, old = VPS.positive(quote.get("price")), VPS.positive(position.get("avg_entry_price")), VPP.effective_stop(position)
    if not px or not entry or not old:
        return out
    sign = 1 if scope["held_direction"] == "LONG" else -1
    if sign * (px - old) <= 0:
        return dict(out, reason="STRUCTURAL_PARENT_EXISTING_STOP_BREACHED")
    observed = TFS.timestamp(quote.get("observed_at"))
    if observed is None or not scope["opened_at"] <= observed <= clock.timestamp():
        return dict(out, reason="STRUCTURAL_PARENT_FRESH_SOURCE_QUOTE_REQUIRED")
    contexts = []
    for row in summary or []:
        if row.get("asset") != asset or not (VPS.same(identity, VPS.identity(asset, row))
                                             and VPS.same(VPS.identity(asset, row), identity)):
            continue
        ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
        if (ctx.get("version") != SB.VERSION or ctx.get("structural_timeframe") != horizon
                or ctx.get("timeframe") != row.get("horizon")):
            continue
        rebound = SB.rebind_quote(ctx, quote, clock)
        structure = SB.management_structure(rebound, clock)
        if structure["eligible"]:
            contexts.append(structure)
    if not contexts:
        return out
    structure = max(contexts, key=lambda x: x["closed_at"])
    atr, policy = structure["atr"], structure["policy"]
    kind = "support" if sign == 1 else "resistance"
    levels = [level for level in structure["levels"] if level["kind"] == kind
              and level["timeframe"] == horizon and level["pivot_at"] >= scope["opened_at"]
              and level["prominence"] >= policy["protected_swing_min_prominence_atr"] * atr
              and sign * (px - level["price"]) > 0]
    if not levels:
        return dict(out, reason="STRUCTURAL_PARENT_NEW_CONFIRMED_SWING_REQUIRED")
    level = max(levels, key=lambda x: (x["pivot_at"], x["available_at"]))
    anchor, pivot = level["price"], level["pivot_at"]
    stop = anchor - sign * policy["stop_buffer_atr"] * atr
    if stop <= 0 or sign * (px - stop) <= 0 or sign * (stop - old) <= 0:
        return dict(out, reason="STRUCTURAL_PARENT_TRAIL_DOES_NOT_IMPROVE_STOP")
    previous = p.get("same_tf_trailing") or {}
    if (TFS.timestamp(previous.get("reference_pivot_at")) or 0) >= pivot:
        return dict(out, reason="STRUCTURAL_PARENT_SWING_ALREADY_MANAGED")
    candidate = dict(out, eligible=True, reason="STRUCTURAL_PARENT_CONFIRMED_SWING_TRAIL",
                     old_stop=old, stop_price=stop, price=px, entry_price=entry, atr=atr,
                     reference_level=anchor, reference_pivot_at=pivot,
                     reference_level_id=level["level_id"], level_available_at=level["available_at"],
                     pivot_right_required=policy["pivot_right"], stop_buffer_atr=policy["stop_buffer_atr"],
                     price_to_stop_atr=sign * (px-stop)/atr, entry_to_stop_atr=sign * (entry-stop)/atr,
                     distance_validation="DIAGNOSTIC_ONLY_NO_NEW_ATR_THRESHOLD",
                     closed_at=structure["closed_at"], source_identity=deepcopy(identity),
                     structural_policy_version=p.get("structural_policy_version"),
                     entry_event_id=scope["entry_event_id"], quote_observed_at=observed,
                     structural_policy=deepcopy(policy), parent_atr_proof=deepcopy(structure["atr_proof"]))
    checked = SB.validate_management_revision(candidate, p.get("entry_event_snapshot") or
                                              (p.get("timeframe_entry_context") or {}).get("event"),
                                              scope["opened_at"], observed)
    return candidate if checked["eligible"] else dict(out, reason=checked["reason"])


def trailing_candidate(position, summary, quote, now):
    """Pure geometry/source check; the entry snapshot and target are untouched."""
    out = {"version": VERSION, "eligible": False, "reason": "LEGACY_POSITION"}
    if not owns_position(position):
        return out
    scope = PT.position_scope(position)
    if scope["quote_structural_binding"]:
        return _parent_trailing_candidate(position, summary, quote, now, scope)
    p = payload(position)
    horizon, asset = p.get("execution_horizon"), position.get("asset")
    direction = position.get("direction")
    clock, opened = utc_datetime(now), TFS.timestamp(position.get("opened_at"))
    identity = VPS.position_identity(position)
    entry_event = p.get("entry_event_snapshot") or {}
    quote = quote or {}
    out.update(reason="SAME_TF_TRAIL_CONTEXT_REQUIRED", timeframe=horizon)
    if (horizon not in TFS.TIMEFRAMES or direction not in ("LONG", "SHORT") or clock is None
            or opened is None or not quote.get("source_gate_pass") or not VPS.matches(position, quote)
            or not quote_gate(quote.get("observed_at"), now=clock, execution=True, asset=asset)["eligible"]):
        return out
    if entry_event.get("timeframe") and entry_event["timeframe"] != horizon:
        return dict(out, reason="SAME_TF_POSITION_HORIZON_CHANGED")
    entry_source = entry_event.get("source_identity")
    if entry_source and not (VPS.same(identity, entry_source) and VPS.same(entry_source, identity)):
        return dict(out, reason="SAME_TF_POSITION_SOURCE_CHANGED")
    px, entry = VPS.positive(quote.get("price")), VPS.positive(position.get("avg_entry_price"))
    old = VPP.effective_stop(position)
    if not px or not entry or not old:
        return out
    sign = 1 if direction == "LONG" else -1
    if sign * (px - old) <= 0:
        return dict(out, reason="SAME_TF_EXISTING_STOP_BREACHED")
    quote_at = TFS.timestamp(quote.get("observed_at"))
    contexts = []
    for row in summary or []:
        if row.get("asset") != asset or row.get("horizon") != horizon:
            continue
        ctx = row.get("timeframe_entry_context") or (row.get("trade_plan") or {}).get("timeframe_entry_context") or {}
        closed = TFS.timestamp(ctx.get("closed_at"))
        if (ctx.get("version") != TFS.VERSION or ctx.get("status") != "OK"
                or ctx.get("timeframe") != horizon or ctx.get("atr_timeframe") != horizon
                or not VPS.same(identity, ctx.get("source_identity"))
                or not VPS.same(ctx.get("source_identity"), identity)
                or not VPS.same(identity, VPS.identity(asset, row))
                or closed is None or closed > min(clock.timestamp(), quote_at)
                or clock.timestamp() - closed > TFS.TIMEFRAMES[horizon]):
            continue
        contexts.append((closed, ctx))
    if not contexts:
        return out
    closed, ctx = max(contexts, key=lambda item: item[0])
    atr = VPS.positive(ctx.get("atr"))
    if not atr:
        return dict(out, reason="SAME_TF_TRAIL_ATR_MISSING")
    seconds = TFS.TIMEFRAMES[horizon]
    right = int(CTC.STRUCTURAL_ENTRY_POLICY["pivot_right"])
    kind = "support" if sign == 1 else "resistance"
    levels = []
    for level in ctx.get("levels") or []:
        available, pivot = TFS.timestamp(level.get("available_at")), TFS.timestamp(level.get("pivot_at"))
        anchor = VPS.positive(level.get("price"))
        if (level.get("kind") == kind and level.get("timeframe") == horizon and anchor
                and available is not None and pivot is not None and pivot >= opened
                # A pivot timestamp denotes the opening of its candle. Its
                # own close plus all right-hand closes must be observed. A
                # newly reported pre-entry extreme is not a new held-trade swing.
                and pivot + (right + 1) * seconds <= available <= closed
                and sign * (px - anchor) > 0):
            levels.append((pivot, available, anchor))
    if not levels:
        return dict(out, reason="SAME_TF_NEW_CONFIRMED_SWING_REQUIRED")
    pivot, available, anchor = max(levels)
    buffer_atr = float(CTC.STRUCTURAL_ENTRY_POLICY["stop_buffer_atr"])
    stop = anchor - sign * buffer_atr * atr
    if stop <= 0 or sign * (px - stop) <= 0 or sign * (stop - old) <= 0:
        return dict(out, reason="SAME_TF_TRAIL_DOES_NOT_IMPROVE_STOP")
    previous = p.get("same_tf_trailing") or {}
    if (TFS.timestamp(previous.get("reference_pivot_at")) or 0) >= pivot:
        return dict(out, reason="SAME_TF_SWING_ALREADY_MANAGED")
    return dict(out, eligible=True, reason="SAME_TF_CONFIRMED_SWING_TRAIL",
                old_stop=old, stop_price=stop, price=px, entry_price=entry, atr=atr,
                reference_level=anchor, reference_pivot_at=pivot, level_available_at=available,
                pivot_right_required=right, stop_buffer_atr=buffer_atr,
                price_to_stop_atr=sign * (px - stop) / atr,
                entry_to_stop_atr=sign * (entry - stop) / atr,
                distance_validation="DIAGNOSTIC_ONLY_NO_NEW_ATR_THRESHOLD",
                closed_at=closed, source_identity=deepcopy(identity),
                structural_policy_version=p["structural_policy_version"])


def apply_trailing(c, name, position, summary, quote, now):
    maturity=payload(position)
    if (CTC.LIFECYCLE_POLICY.get("structural_trailing_before_profit_maturity") is False
            and not maturity.get("profit_maturity_armed")):
        return {"version":VERSION,"eligible":False,
                "reason":"PROFIT_MATURITY_NOT_CONFIRMED",
                "positive_streak":int(maturity.get("profit_maturity_positive_streak") or 0),
                "required_windows":int(CTC.LIFECYCLE_POLICY.get("profit_lock_requires_consecutive_positive_windows") or 3)}
    candidate = trailing_candidate(position, summary, quote, now)
    if not candidate.get("eligible"):
        return candidate
    try:
        protection = VPP.assess(c, position, stop=candidate["stop_price"],
                                price=candidate["price"], now=now)
    except Exception:
        # Geometry, source, freshness and no-widening already passed. Missing
        # accounting must not retain a larger structural loss, and must never
        # leave a previous claim of protected net profit active.
        protection = VPP.evaluate(position, None, stop=candidate["stop_price"],
                                  price=candidate["price"], now=now)
    # Structural risk reduction is valid even if this tighter stop still exits
    # at a small net loss after costs. VPP alone labels verified profit; crossing
    # the entry price is not a condition for moving a confirmed same-TF stop.
    event = dict(candidate, at=utc_datetime(now).isoformat())
    history = list(payload(position).get("same_tf_trailing_history") or [])
    rule = ("CTC_PROTECTED_PARENT_SWING" if candidate.get("management_rule") == "PROTECTED_PARENT_SWING"
            else "CTC_SAME_TF_CONFIRMED_SWING")
    patch = {**protection, "trailing_stop": candidate["stop_price"],
             "trailing_rule": rule, "trailing_reference_timeframe": candidate["timeframe"],
             "same_tf_trailing": event, "same_tf_trailing_history": (history + [event])[-32:],
             "r48_profit_lock_active": False, "r55_net_profit_lock_active": False}
    encoded = json.dumps(patch, ensure_ascii=False, allow_nan=False)
    # Another manager can tighten the stop or resize the remainder after the
    # caller reads its snapshot. Atomically match that snapshot before writing;
    # otherwise an apparently tighter candidate could widen the persisted stop
    # or publish net-profit accounting for a different quantity/entry price.
    changed = c.execute(
        "UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
        "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s AND direction=%s "
        "AND stop_price IS NOT DISTINCT FROM %s AND units IS NOT DISTINCT FROM %s "
        "AND avg_entry_price IS NOT DISTINCT FROM %s "
        "AND COALESCE(payload->'trailing_stop','null'::jsonb)=%s::jsonb RETURNING active_trade_id",
        (candidate["stop_price"], encoded, name, position.get("asset"), position.get("active_trade_id"),
         position.get("direction"), position.get("stop_price"), position.get("units"),
         position.get("avg_entry_price"), json.dumps(payload(position).get("trailing_stop")))).fetchone()
    if not changed:
        return dict(candidate, eligible=False, reason="SAME_TF_POSITION_CHANGED")
    if position.get("active_trade_id"):
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (encoded, position["active_trade_id"]))
    return event
