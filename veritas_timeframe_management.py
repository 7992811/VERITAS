"""Same-timeframe structural management for explicitly tagged new positions.

Legacy positions keep their existing policy. A new closed, confirmed swing on
the entry timeframe can reduce risk; no quote-only or lower-timeframe stop can.
"""
import json
from copy import deepcopy

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
import veritas_profit_protection as VPP
import veritas_timeframe_structure as TFS
from veritas_quote_time import quote_gate, utc_datetime

VERSION = "CTC_SAME_TF_MANAGEMENT_V1"


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
    asset, horizon = position.get("asset"), payload(position).get("execution_horizon")
    seconds = TFS.TIMEFRAMES.get(horizon)
    if seconds is None:
        book.pop(asset, None)
        return book, [r for r in rows if r.get("asset") != asset]
    def usable(row):
        return row.get("asset") != asset or TFS.TIMEFRAMES.get(row.get("horizon"), 0) >= seconds
    if asset in book and not usable(book[asset]):
        book.pop(asset)
    return book, [r for r in rows if usable(r)]


def trailing_candidate(position, summary, quote, now):
    """Pure geometry/source check; the entry snapshot and target are untouched."""
    out = {"version": VERSION, "eligible": False, "reason": "LEGACY_POSITION"}
    if not owns_position(position):
        return out
    p = payload(position)
    horizon, asset = p.get("execution_horizon"), position.get("asset")
    direction = position.get("direction")
    clock, opened = utc_datetime(now), TFS.timestamp(position.get("opened_at"))
    identity = VPS.position_identity(position)
    quote = quote or {}
    out.update(reason="SAME_TF_TRAIL_CONTEXT_REQUIRED", timeframe=horizon)
    if (horizon not in TFS.TIMEFRAMES or direction not in ("LONG", "SHORT") or clock is None
            or opened is None or not quote.get("source_gate_pass") or not VPS.matches(position, quote)
            or not quote_gate(quote.get("observed_at"), now=clock, execution=True, asset=asset)["eligible"]):
        return out
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
    kind = "support" if sign == 1 else "resistance"
    levels = []
    for level in ctx.get("levels") or []:
        available, pivot = TFS.timestamp(level.get("available_at")), TFS.timestamp(level.get("pivot_at"))
        anchor = VPS.positive(level.get("price"))
        if (level.get("kind") == kind and level.get("timeframe") == horizon and anchor
                and available is not None and pivot is not None and pivot < available <= closed
                and available > opened and sign * (px - anchor) > 0):
            levels.append((pivot, available, anchor))
    if not levels:
        return dict(out, reason="SAME_TF_NEW_CONFIRMED_SWING_REQUIRED")
    pivot, available, anchor = max(levels)
    stop = anchor - sign * float(CTC.STRUCTURAL_ENTRY_POLICY["stop_buffer_atr"]) * atr
    if stop <= 0 or sign * (px - stop) <= 0 or sign * (stop - old) <= 0:
        return dict(out, reason="SAME_TF_TRAIL_DOES_NOT_IMPROVE_STOP")
    previous = p.get("same_tf_trailing") or {}
    if (TFS.timestamp(previous.get("reference_pivot_at")) or 0) >= pivot:
        return dict(out, reason="SAME_TF_SWING_ALREADY_MANAGED")
    return dict(out, eligible=True, reason="SAME_TF_CONFIRMED_SWING_TRAIL",
                old_stop=old, stop_price=stop, price=px, entry_price=entry, atr=atr,
                reference_level=anchor, reference_pivot_at=pivot, level_available_at=available,
                closed_at=closed, source_identity=deepcopy(identity),
                structural_policy_version=p["structural_policy_version"])


def apply_trailing(c, name, position, summary, quote, now):
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
    patch = {**protection, "trailing_stop": candidate["stop_price"],
             "trailing_rule": "CTC_SAME_TF_CONFIRMED_SWING", "trailing_reference_timeframe": candidate["timeframe"],
             "same_tf_trailing": event, "same_tf_trailing_history": (history + [event])[-32:],
             "r48_profit_lock_active": False, "r55_net_profit_lock_active": False}
    encoded = json.dumps(patch, ensure_ascii=False, allow_nan=False)
    c.execute("UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
              "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s",
              (candidate["stop_price"], encoded, name, position.get("asset"), position.get("active_trade_id")))
    if position.get("active_trade_id"):
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (encoded, position["active_trade_id"]))
    return event
