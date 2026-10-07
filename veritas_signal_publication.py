"""Publish completed signal cells without waiting for portfolio accounting.

Only the matrix and explicit partial-cycle metadata change here. Quote, event,
decision and completed-cycle clocks retain their original meanings. Compact
rows and immutable proof objects are borrowed; no candle graph is copied.
"""
from datetime import datetime, timezone
import math

import veritas_canonical_constitution as CTC
import veritas_price_source as VPS
from veritas_timeframe_structure import timestamp

ASSETS = ("BTC", "ETH", "NQ", "BRENT", "GOLD", "MOEX", "CNYRUBF")
HORIZONS = ("1m", "5m", "1h", "4h", "1d", "3d", "7d")
MAX_CELLS = 49


def _quote(row):
    if "_execution_quote" in row:
        quote = row.get("_execution_quote")
        return quote if isinstance(quote, dict) else {}
    return row


def _observed(row):
    quote = _quote(row)
    if quote is row:
        return timestamp(row.get("market_observed_at") or row.get("observed_at"))
    return timestamp(quote.get("observed_at"))


def _decision_at(row):
    """An original admission/plan clock, never the publication clock."""
    plan = row.get("trade_plan") or {}
    if not isinstance(plan, dict):
        return None
    economics = plan.get("final_economics_gate") or {}
    economics = economics if isinstance(economics, dict) else {}
    for value in (row.get("trade_entry_checked_at"), plan.get("trade_entry_checked_at"),
                  economics.get("checked_at"), plan.get("decision_as_of"),
                  row.get("_breakout_checked_at")):
        stamp = timestamp(value)
        if stamp is not None:
            return stamp
    return None


def _event_key(row):
    plan = row.get("trade_plan") or {}
    if not isinstance(plan, dict):
        return None
    context = row.get("timeframe_entry_context") or plan.get("timeframe_entry_context") or {}
    if not isinstance(context, dict):
        return None
    event = context.get("event") or plan.get("entry_event_snapshot") or {}
    if not isinstance(event, dict):
        return None
    event_id = event.get("event_id") or plan.get("entry_event_id")
    direction = row.get("research_decision") or row.get("decision")
    source = VPS.identity(row.get("asset"), _quote(row))
    if (not event_id or not source or direction not in ("LONG", "SHORT")
            or event.get("direction", direction) != direction
            or plan.get("entry_event_id", event_id) != event_id
            or context.get("timeframe", row.get("horizon")) != row.get("horizon")
            or event.get("timeframe", row.get("horizon")) != row.get("horizon")
            or event.get("asset", row.get("asset")) != row.get("asset")):
        return None
    for identity in (context.get("source_identity"), event.get("source_identity")):
        if identity and (not isinstance(identity, dict) or (
                identity.get("key"), identity.get("contract_id")) != (
                source.get("key"), source.get("contract_id"))):
            return None
    return (row.get("asset"), row.get("horizon"), event_id, direction,
            source.get("key"), source.get("contract_id"))


def _scalars(value, fields):
    value = value if isinstance(value, dict) else {}
    result = {}
    for key in fields:
        item = value.get(key)
        if item is None or isinstance(item, (bool, int)):
            if key in value:
                result[key] = item
        elif isinstance(item, str):
            result[key] = item[:400]
        elif isinstance(item, float) and math.isfinite(item):
            result[key] = item
    return result


def _executions(row):
    """Only exact selected-event fills, never another horizon's book graph."""
    key = _event_key(row)
    if key is None:
        return {}
    audit = row.get("_execution_audit") or {}
    if not isinstance(audit, dict):
        return {}
    result = audit.get("result") or {}
    if not isinstance(result, dict):
        return {}
    selected = {}
    for item in result.get("portfolios") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if (name not in CTC.PORTFOLIO_ORDER or item.get("status") != "EXECUTED"
                or (item.get("asset"), item.get("horizon"), item.get("event_id")) != key[:3]):
            continue
        selected[name] = _scalars(item, ("name", "asset", "horizon", "event_id", "status",
            "reason", "checked_at", "execution_action", "fill_price", "order_id",
            "current_fraction", "requested_fraction", "target_fraction"))
    return selected


def _retain_execution(previous, candidate):
    """A later quote for the same event does not erase its actual fill."""
    key = _event_key(previous)
    if key is None or key != _event_key(candidate):
        return candidate
    old = _executions(previous)
    if not old:
        return candidate
    new = _executions(candidate)
    changed = False
    for name, item in old.items():
        if (name not in new or (timestamp(item.get("checked_at")) or 0)
                > (timestamp(new[name].get("checked_at")) or 0)):
            new[name] = item
            changed = True
    if not changed:
        return candidate
    # The enclosing audit clock is the stored audit clock, never publication.
    base = candidate if _executions(candidate) else previous
    audit = base.get("_execution_audit") or {}
    result = _scalars(audit.get("result"), ("status", "reason", "version", "checked_at", "paper_only"))
    result["portfolios"] = [new[name] for name in CTC.PORTFOLIO_ORDER if name in new]
    out = dict(candidate)
    out["_execution_audit"] = dict(_scalars(audit, ("lane", "checked_at", "paper_only")), result=result)
    return out


def prefer_row(previous, candidate):
    """Order by observed quote, then original decision time for equal quotes."""
    if previous is None:
        return dict(candidate)
    old_at, new_at = _observed(previous), _observed(candidate)
    if old_at is not None and (new_at is None or new_at < old_at):
        return _retain_execution(candidate, previous)
    if old_at == new_at:
        old_check, new_check = _decision_at(previous), _decision_at(candidate)
        if old_check is not None and (new_check is None or new_check < old_check):
            return _retain_execution(candidate, previous)
        newer_decision = new_check is not None and (old_check is None or new_check > old_check)
        if previous.get("_breakout_runtime") and not newer_decision:
            # A finishing cycle may only have changed snapshot_stale. A truly
            # later refusal on this same quote must still replace the old plan.
            return _retain_execution(candidate, previous)
    return _retain_execution(previous, dict(candidate))


def merge_rows(previous, incoming, *, assets=ASSETS, horizons=HORIZONS):
    """A deterministic bounded matrix, preserving untouched cell objects."""
    order = [(asset, tf) for asset in assets for tf in horizons][:MAX_CELLS]
    allowed, indexed = set(order), {}
    for initial, rows in ((True, previous), (False, incoming)):
        for row in rows or ():
            if not isinstance(row, dict):
                continue
            key = (row.get("asset"), row.get("horizon"))
            if key in allowed:
                indexed[key] = row if initial and key not in indexed else prefer_row(indexed.get(key), row)
    return [indexed[key] for key in order if key in indexed]


def publish_completed(ns, rows, cycle_id, cycle_mode):
    """Expose finished compact cells while the ordinary cycle is still busy."""
    stamp = ns["now"]() if callable(ns.get("now")) else datetime.now(timezone.utc).isoformat()
    completed = [dict(row, snapshot_stale=False) for row in rows or () if isinstance(row, dict)]
    with ns["lock"]:
        last = ns.setdefault("last_cycle", {})
        before = {(row.get("asset"), row.get("horizon")):row for row in last.get("summary") or []
                  if isinstance(row, dict)}
        merged = merge_rows(last.get("summary"), completed,
            assets=ns.get("DISPLAY_ASSETS", ASSETS), horizons=ns.get("HORIZONS", HORIZONS))
        published = sum(row is not before.get((row.get("asset"), row.get("horizon"))) for row in merged)
        last["summary"] = merged
        if published:
            last["signals_updated_at"] = stamp
        last["cycle_in_progress"] = {"cycle_id": cycle_id, "cycle_mode": cycle_mode}
    return published


def finish_summary(state, previous, *, assets=ASSETS, horizons=HORIZONS):
    """Call under the publication lock before replacing a completed cycle."""
    state["summary"] = merge_rows(previous.get("summary"), state.get("summary"),
                                  assets=assets, horizons=horizons)
    state["signals_updated_at"] = previous.get("signals_updated_at")
    state["cycle_in_progress"] = False
    return state["summary"]
