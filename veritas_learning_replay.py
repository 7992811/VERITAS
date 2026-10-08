"""Same-source replay evidence for VERITAS Learning 2.0 stop/exit candidates.

This module never creates orders. It reconstructs only preregistered candidate
policies on chronological OHLC from the exact provider family used by the
original paper trade. Unsupported/mismatched sources remain REPLAY_REQUIRED.
Intrabar stop/target ambiguity is excluded instead of guessed.
"""
from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timezone
import math
import os

import veritas_learning_v2 as V2
import veritas_price_source as VPS

VERSION = "LEARNING_V2_SAME_SOURCE_REPLAY_V1"
ROUNDTRIP_COST = .0016
HIGH_COST = .0040
MAX_CANDIDATES_PER_RUN = 2
MAX_TRADES_PER_CANDIDATE = 500


def _num(v):
    if v is None or isinstance(v, bool):
        return None
    try:
        x = float(v)
        return x if math.isfinite(x) else None
    except Exception:
        return None


def _dt(v):
    if isinstance(v, datetime):
        return v.astimezone(timezone.utc) if v.tzinfo else None
    try:
        x = datetime.fromisoformat(str(v).replace("Z", "+00:00"))
        return x.astimezone(timezone.utc) if x.tzinfo else None
    except Exception:
        return None


def _payload(v):
    return v if isinstance(v, dict) else {}


def _identity(payload):
    payload = _payload(payload)
    value = payload.get("price_source_lock") or payload.get("entry_execution_source_identity")
    return value if isinstance(value, dict) else {}


def _bar(row):
    if isinstance(row, dict):
        start = _num(row.get("ts", row.get("start_ts", row.get("open_time"))))
        end = _num(row.get("end_ts", row.get("close_ts", row.get("close_time"))))
        o = _num(row.get("open")); h = _num(row.get("high"))
        l = _num(row.get("low")); c = _num(row.get("close"))
        if start is not None and start > 10**12:
            start /= 1000.0
        if end is not None and end > 10**12:
            end /= 1000.0
        if end is None and start is not None:
            end = start
    elif isinstance(row, (list, tuple)) and len(row) >= 5:
        start = _num(row[0]); o = _num(row[1]); h = _num(row[2])
        l = _num(row[3]); c = _num(row[4])
        end = _num(row[6]) if len(row) > 6 else start
        if start is not None and start > 10**12:
            start /= 1000.0
        if end is not None and end > 10**12:
            end /= 1000.0
    else:
        return None
    if None in (start, end, o, h, l, c) or min(o, h, l, c) <= 0 or h < max(o, c, l) or l > min(o, c, h):
        return None
    return {"start": float(start), "end": float(end), "open": o, "high": h, "low": l, "close": c}


def _bars(rows):
    out = []
    for row in rows or []:
        b = _bar(row)
        if b:
            out.append(b)
    out.sort(key=lambda x: (x["start"], x["end"]))
    return out


def _touch(bar, level, direction, kind):
    if kind == "stop":
        return bar["low"] <= level if direction == "LONG" else bar["high"] >= level
    return bar["high"] >= level if direction == "LONG" else bar["low"] <= level


def _exit_fill(bar, level, direction, kind):
    # Gap through a stop is adverse; profit targets never assume improvement.
    if kind == "stop":
        if direction == "LONG" and bar["open"] < level:
            return bar["open"]
        if direction == "SHORT" and bar["open"] > level:
            return bar["open"]
    return level


def _signed_return(entry, exit_price, direction):
    return (exit_price / entry - 1.0) if direction == "LONG" else (entry / exit_price - 1.0)


def replay_full(entry, stop, target, direction, bars, *, cost=ROUNDTRIP_COST):
    """Full-size baseline: stop/target/horizon, excluding ambiguous OHLC bars."""
    if not bars or direction not in ("LONG", "SHORT") or min(entry, stop, target) <= 0:
        return {"eligible": False, "reason": "INVALID_REPLAY_INPUT"}
    for bar in bars:
        st = _touch(bar, stop, direction, "stop")
        tp = _touch(bar, target, direction, "target")
        if st and tp:
            return {"eligible": False, "reason": "INTRABAR_ORDER_AMBIGUOUS"}
        if st:
            px = _exit_fill(bar, stop, direction, "stop")
            gross = _signed_return(entry, px, direction)
            return {"eligible": True, "reason": "STOP", "gross": gross,
                    "net": gross - cost, "exit_price": px, "exit_at": bar["end"]}
        if tp:
            px = target
            gross = _signed_return(entry, px, direction)
            return {"eligible": True, "reason": "TARGET", "gross": gross,
                    "net": gross - cost, "exit_price": px, "exit_at": bar["end"]}
    px = bars[-1]["close"]
    gross = _signed_return(entry, px, direction)
    return {"eligible": True, "reason": "HORIZON", "gross": gross,
            "net": gross - cost, "exit_price": px, "exit_at": bars[-1]["end"]}


def replay_wider_stop(entry, stop, target, direction, bars, *, atr, delta_atr=.05,
                      cost=ROUNDTRIP_COST):
    sign = 1.0 if direction == "LONG" else -1.0
    widened = stop - sign * float(delta_atr) * float(atr)
    old_risk = sign * (entry - stop)
    new_risk = sign * (entry - widened)
    if old_risk <= 0 or new_risk <= old_risk:
        return {"eligible": False, "reason": "INVALID_WIDER_STOP_GEOMETRY"}
    result = replay_full(entry, widened, target, direction, bars, cost=cost)
    if not result.get("eligible"):
        return result
    factor = min(1.0, old_risk / new_risk)
    result = dict(result, stop_price=widened, risk_neutral_size=factor,
                  gross=result["gross"] * factor,
                  net=(result["gross"] - cost) * factor)
    return result


def replay_partial_1r(entry, stop, target, direction, bars, *, partial=.25,
                      trigger_r=1.0, cost=ROUNDTRIP_COST):
    sign = 1.0 if direction == "LONG" else -1.0
    risk = sign * (entry - stop)
    if risk <= 0 or not 0 < partial < 1 or trigger_r <= 0:
        return {"eligible": False, "reason": "INVALID_PARTIAL_GEOMETRY"}
    trigger = entry + sign * risk * trigger_r
    if sign * (target - trigger) <= 0:
        return {"eligible": False, "reason": "PARTIAL_TRIGGER_NOT_BEFORE_TARGET"}
    realized = 0.0
    remaining = 1.0
    hit_partial = False
    for bar in bars:
        st = _touch(bar, stop, direction, "stop")
        pt = (not hit_partial) and _touch(bar, trigger, direction, "target")
        tp = _touch(bar, target, direction, "target")
        if st and (pt or tp):
            return {"eligible": False, "reason": "INTRABAR_ORDER_AMBIGUOUS"}
        if st:
            px = _exit_fill(bar, stop, direction, "stop")
            gross = realized + remaining * _signed_return(entry, px, direction)
            return {"eligible": True, "reason": "STOP", "gross": gross, "net": gross - cost,
                    "partial_hit": hit_partial, "exit_price": px, "exit_at": bar["end"]}
        if pt:
            realized += partial * _signed_return(entry, trigger, direction)
            remaining -= partial
            hit_partial = True
        if tp:
            gross = realized + remaining * _signed_return(entry, target, direction)
            return {"eligible": True, "reason": "TARGET", "gross": gross, "net": gross - cost,
                    "partial_hit": hit_partial, "exit_price": target, "exit_at": bar["end"]}
    px = bars[-1]["close"]
    gross = realized + remaining * _signed_return(entry, px, direction)
    return {"eligible": True, "reason": "HORIZON", "gross": gross, "net": gross - cost,
            "partial_hit": hit_partial, "exit_price": px, "exit_at": bars[-1]["end"]}


def _window_seconds(horizon):
    # Preregistered evaluation windows, independent of realized trade outcome.
    return {"1m": 3600, "5m": 4 * 3600, "1h": 24 * 3600, "4h": 3 * 86400,
            "1d": 7 * 86400, "3d": 15 * 86400, "7d": 35 * 86400}.get(str(horizon), 24 * 3600)


def _slice(bars, opened, horizon):
    start = opened.timestamp()
    end = start + _window_seconds(horizon)
    # Do not use the partial bar containing entry; it includes pre-entry extremes.
    return [b for b in bars if b["start"] >= start and b["start"] < end]


def _provider_rows(ns, asset, horizon, identity):
    key = str((identity or {}).get("key") or "").upper()
    if key.startswith("PROFINANCE:") and horizon in ("1m", "5m", "1h", "4h", "1d"):
        import veritas_profinance_history as PF
        bundle = PF.fetch_history_bundle(asset=asset, timeframes=(horizon,), budget_seconds=8.)
        if not VPS.same(identity, bundle.get("source_identity")) or not VPS.same(bundle.get("source_identity"), identity):
            return [], "SOURCE_IDENTITY_MISMATCH"
        return _bars((bundle.get("bars_by_timeframe") or {}).get(horizon) or []), "PROFINANCE_SAME_SOURCE"
    symbol = next((sym for sym, (a, _) in ns.get("ASSETS", {}).items() if a == asset), None)
    if key.startswith("BINANCE:") and symbol in ("BTCUSDT", "ETHUSDT") and horizon not in ("1m", "5m"):
        return _bars(ns["_fetch_history"](symbol, 120)), "BINANCE_SAME_SOURCE_1H"
    if key.startswith("MOEX:") and asset in ("MOEX", "CNYRUBF") and horizon not in ("1m", "5m"):
        return _bars(ns["_fetch_history"](asset, 180)), "MOEX_SAME_SOURCE_1H"
    return [], "UNSUPPORTED_EXACT_SOURCE_REPLAY"


def _decision_rows(pg_connect, candidate):
    """Read only frozen decision evidence belonging to the candidate cohort."""
    scope = candidate.get("scope") or {}
    with pg_connect() as c:
        rows = c.execute("""SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload,
          e.regime,e.decision,e.forward_return
          FROM ledger_events d
          JOIN v90_decision_episodes e ON e.entity_key=d.entity_key
          WHERE d.event_type='decision' AND d.asset=%s AND d.horizon=%s
            AND COALESCE(e.regime,'UNKNOWN')=%s
          ORDER BY d.event_ts DESC LIMIT %s""",
          (scope.get("asset"), scope.get("horizon"), scope.get("regime"),
           MAX_TRADES_PER_CANDIDATE)).fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        p = _payload(row.get("payload"))
        if V2._source_key(p) != scope.get("source_key") or V2._policy_hash(p) != scope.get("policy_hash"):
            continue
        if str(row.get("decision") or "") != "NO_TRADE":
            continue
        blockers = set(V2._blockers(p))
        wanted = str((candidate.get("proposal") or {}).get("soft_blocker") or "")
        if wanted and wanted not in blockers:
            continue
        out.append(row)
    out.sort(key=lambda r: (_dt(r.get("event_ts")) or datetime.min.replace(tzinfo=timezone.utc),
                            str(r.get("entity_key") or "")))
    return out


def _decision_geometry(row, candidate):
    p = _payload(row.get("payload"))
    provenance = _payload(p.get("learning_provenance"))
    quote = _payload(provenance.get("quote"))
    context = _payload(p.get("timeframe_entry_context"))
    event = _payload(context.get("event"))
    if not event:
        event = _payload((_payload(p.get("trade_plan"))).get("entry_event_snapshot"))
    entry = _num(quote.get("price"))
    stop = _num(event.get("stop_price") or p.get("stop_price"))
    target = _num(event.get("target_price") or p.get("target_price"))
    direction = str((candidate.get("scope") or {}).get("direction") or event.get("direction") or "")
    decided = _dt(provenance.get("decision_at") or row.get("event_ts"))
    identity = quote.get("source_identity")
    if not isinstance(identity, dict):
        identity = _identity(p)
    if not all((entry, stop, target, decided)) or direction not in ("LONG", "SHORT"):
        return None
    if not isinstance(identity, dict) or not identity.get("key"):
        return None
    # Geometry must point in the declared direction at decision time.
    if direction == "LONG" and not (stop < entry < target):
        return None
    if direction == "SHORT" and not (target < entry < stop):
        return None
    return {"entry": entry, "stop": stop, "target": target,
            "direction": direction, "opened": decided, "identity": identity}


def replay_entry_candidate(ns, pg_connect, candidate):
    """Counterfactual for a verified false block: NO_TRADE versus frozen plan."""
    rows = _decision_rows(pg_connect, candidate)
    if not rows:
        return {"status": "NO_MATCHING_DECISIONS",
                "candidate_id": candidate.get("candidate_id"), "rows": 0}
    by_identity = defaultdict(list)
    exclusions = defaultdict(int)
    for row in rows:
        g = _decision_geometry(row, candidate)
        if not g:
            exclusions["INCOMPLETE_FROZEN_ENTRY_GEOMETRY"] += 1
            continue
        token = (g["identity"].get("key"), g["identity"].get("contract_id"))
        by_identity[token].append((row, g))
    replay = []
    for _, group in by_identity.items():
        identity = group[0][1]["identity"]
        bars, source_status = _provider_rows(
            ns, (candidate.get("scope") or {}).get("asset"),
            (candidate.get("scope") or {}).get("horizon"), identity)
        if not bars:
            exclusions[source_status] += len(group)
            continue
        for row, g in group:
            window = _slice(bars, g["opened"], (candidate.get("scope") or {}).get("horizon"))
            if len(window) < 2:
                exclusions["HISTORY_WINDOW_UNAVAILABLE"] += 1
                continue
            alt = replay_full(g["entry"], g["stop"], g["target"], g["direction"], window)
            if not alt.get("eligible"):
                exclusions["ALT_" + str(alt.get("reason"))] += 1
                continue
            replay.append({
                "trade_id": row["entity_key"], "at": g["opened"],
                "baseline_net": 0.0, "candidate_net": alt["net"],
                "candidate_high_cost": alt["gross"] - HIGH_COST,
                "delta": alt["net"], "parity": True,
                "source_status": source_status,
            })
    if not replay:
        return {"status": "INSUFFICIENT_REPLAY_EVIDENCE",
                "candidate_id": candidate.get("candidate_id"), "rows": 0,
                "exclusions": dict(exclusions)}
    evidence = _evidence(candidate, replay, abstention_baseline=True)
    result = V2.record_replay_evidence(pg_connect, candidate["candidate_id"], evidence)
    return {"status": result.get("status"), "candidate_id": candidate.get("candidate_id"),
            "rows": len(replay), "delta_metrics": _metrics(replay, "delta"),
            "exclusions": dict(exclusions), "gate": result.get("gate")}


def _trades(pg_connect, candidate):
    scope = candidate.get("scope") or {}
    with pg_connect() as c:
        rows = c.execute("""SELECT t.trade_id,t.opened_at,t.closed_at,t.asset,t.horizon,t.direction,
          t.avg_entry_price,t.payload,e.regime,e.learning_eligible
          FROM paper_trades t JOIN v90_learning_episodes e ON e.trade_id=t.trade_id
          WHERE e.learning_eligible=TRUE AND t.asset=%s AND COALESCE(t.horizon,'')=%s
            AND COALESCE(e.regime,'UNKNOWN')=%s
          ORDER BY t.opened_at DESC LIMIT %s""",
          (scope.get("asset"), scope.get("horizon"), scope.get("regime"),
           MAX_TRADES_PER_CANDIDATE)).fetchall()
    out = []
    for raw in rows:
        row = dict(raw)
        p = _payload(row.get("payload"))
        if V2._source_key(p) != scope.get("source_key") or V2._policy_hash(p) != scope.get("policy_hash"):
            continue
        out.append(row)
    out.sort(key=lambda r: (_dt(r.get("opened_at")) or datetime.min.replace(tzinfo=timezone.utc),
                            str(r.get("trade_id") or "")))
    return out


def _trade_geometry(row):
    p = _payload(row.get("payload"))
    event = _payload(p.get("entry_event_snapshot"))
    fill = _payload(p.get("entry_execution_model"))
    entry = _num(fill.get("fill_price")) or _num(row.get("avg_entry_price"))
    stop = _num(p.get("initial_stop_price"))
    target = _num(event.get("target_price"))
    atr = _num(event.get("atr")) or _num(p.get("entry_atr"))
    direction = str(row.get("direction") or "")
    opened = _dt(row.get("opened_at"))
    identity = _identity(p)
    if not all((entry, stop, target, atr, opened, identity.get("key"))) or direction not in ("LONG", "SHORT"):
        return None
    return {"entry": entry, "stop": stop, "target": target, "atr": atr,
            "direction": direction, "opened": opened, "identity": identity}


def _metrics(rows, key):
    return V2._metrics([r[key] for r in rows if _num(r.get(key)) is not None])


def _evidence(candidate, rows, *, abstention_baseline=False):
    created = V2._now(candidate.get("created_at"))
    historical = [r for r in rows if created is None or r["at"] <= created]
    shadow = [r for r in rows if created is not None and r["at"] > created]
    _train, oos, vault = V2._split(historical)
    mo, mv, ms = _metrics(oos, "candidate_net"), _metrics(vault, "candidate_net"), _metrics(shadow, "candidate_net")
    bo, bv, bs = _metrics(oos, "baseline_net"), _metrics(vault, "baseline_net"), _metrics(shadow, "baseline_net")
    high = V2._metrics([r["candidate_high_cost"] for r in oos + vault])
    return {
      "model_version": VERSION + ":" + str(candidate.get("candidate_id") or "")[:12],
      "oos_n": mo["n"], "oos_expectancy": float(mo["expectancy"] or 0.0),
      "oos_profit_factor": float(mo["profit_factor"] or 0.0),
      "vault_n": mv["n"], "vault_expectancy": float(mv["expectancy"] or 0.0),
      "vault_profit_factor": float(mv["profit_factor"] or 0.0),
      "high_cost_expectancy": float(high["expectancy"] or 0.0),
      "calibration_n": 0, "ece": None, "calibration_applicable": False,
      "requires_baseline_outperformance": True,
      "baseline_oos_expectancy": 0.0 if abstention_baseline else bo["expectancy"],
      "baseline_oos_profit_factor": 1.0 if abstention_baseline else bo["profit_factor"],
      "baseline_vault_expectancy": 0.0 if abstention_baseline else bv["expectancy"],
      "baseline_vault_profit_factor": 1.0 if abstention_baseline else bv["profit_factor"],
      "baseline_shadow_expectancy": 0.0 if abstention_baseline else bs["expectancy"],
      "shadow_trades": ms["n"], "shadow_expectancy": float(ms["expectancy"] or 0.0),
      "shadow_max_drawdown": float(ms["max_drawdown"] or 0.0),
      "code_ci_pass": os.getenv("VERITAS_CODE_CI_PASS", "0").lower() in ("1", "true", "yes", "on"),
      "data_parity_pass": bool(rows and all(r.get("parity") is True for r in rows)),
    }


def replay_candidate(ns, pg_connect, candidate):
    if candidate.get("kind") == "ENTRY_FALSE_BLOCK":
        return replay_entry_candidate(ns, pg_connect, candidate)
    scope = candidate.get("scope") or {}
    proposal = candidate.get("proposal") or {}
    trades = _trades(pg_connect, candidate)
    if not trades:
        return {"status": "NO_MATCHING_TRADES", "candidate_id": candidate.get("candidate_id"), "rows": 0}
    by_identity = defaultdict(list)
    for trade in trades:
        g = _trade_geometry(trade)
        if g:
            token = (g["identity"].get("key"), g["identity"].get("contract_id"))
            by_identity[token].append((trade, g))
    replay = []
    exclusions = defaultdict(int)
    for _, group in by_identity.items():
        identity = group[0][1]["identity"]
        bars, source_status = _provider_rows(ns, scope.get("asset"), scope.get("horizon"), identity)
        if not bars:
            exclusions[source_status] += len(group)
            continue
        for trade, g in group:
            window = _slice(bars, g["opened"], scope.get("horizon"))
            if len(window) < 2:
                exclusions["HISTORY_WINDOW_UNAVAILABLE"] += 1
                continue
            base = replay_full(g["entry"], g["stop"], g["target"], g["direction"], window)
            if not base.get("eligible"):
                exclusions["BASE_" + str(base.get("reason"))] += 1
                continue
            if candidate.get("kind") == "STOP_STRUCTURE":
                alt = replay_wider_stop(
                    g["entry"], g["stop"], g["target"], g["direction"], window,
                    atr=g["atr"], delta_atr=float(proposal.get("stop_buffer_atr_delta") or .05))
            elif candidate.get("kind") == "EXIT_CAPTURE":
                alt = replay_partial_1r(
                    g["entry"], g["stop"], g["target"], g["direction"], window,
                    partial=float(proposal.get("partial_fraction") or .25),
                    trigger_r=float(proposal.get("partial_trigger_r") or 1.0))
            else:
                return {"status": "UNSUPPORTED_KIND", "candidate_id": candidate.get("candidate_id"), "rows": 0}
            if not alt.get("eligible"):
                exclusions["ALT_" + str(alt.get("reason"))] += 1
                continue
            replay.append({
                "trade_id": trade["trade_id"], "at": g["opened"],
                "baseline_net": base["net"], "candidate_net": alt["net"],
                "candidate_high_cost": alt["gross"] - HIGH_COST,
                "delta": alt["net"] - base["net"], "parity": True,
                "source_status": source_status,
            })
    if not replay:
        return {"status": "INSUFFICIENT_REPLAY_EVIDENCE",
                "candidate_id": candidate.get("candidate_id"), "rows": 0,
                "exclusions": dict(exclusions)}
    evidence = _evidence(candidate, replay)
    result = V2.record_replay_evidence(pg_connect, candidate["candidate_id"], evidence)
    return {"status": result.get("status"), "candidate_id": candidate.get("candidate_id"),
            "rows": len(replay), "delta_metrics": _metrics(replay, "delta"),
            "exclusions": dict(exclusions), "gate": result.get("gate")}


def run(ns, pg_connect, *, max_candidates=MAX_CANDIDATES_PER_RUN):
    V2.ensure_schema(pg_connect)
    with pg_connect() as c:
        rows = c.execute("""SELECT * FROM learning_v2_candidates
          WHERE kind IN ('ENTRY_FALSE_BLOCK','STOP_STRUCTURE','EXIT_CAPTURE')
            AND state IN ('REPLAY_REQUIRED','SHADOW')
          ORDER BY updated_at ASC LIMIT %s""",
          (max(1, min(8, int(max_candidates))),)).fetchall()
    results = []
    for raw in rows:
        candidate = dict(raw)
        for key in ("scope", "proposal", "metrics"):
            if isinstance(candidate.get(key), str):
                import json
                candidate[key] = json.loads(candidate[key])
        try:
            results.append(replay_candidate(ns, pg_connect, candidate))
        except Exception as exc:
            results.append({"status": "ERROR", "candidate_id": candidate.get("candidate_id"),
                            "error": f"{type(exc).__name__}: {exc}"})
    return {"version": VERSION, "status": "OK", "candidates": len(results), "results": results,
            "real_order_authority": False, "intrabar_ambiguity_policy": "EXCLUDE"}
