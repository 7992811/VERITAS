"""VERITAS Learning 2.0: bounded strategy evolution with prospective evidence.

This module turns verified decision/trade outcomes into explicit hypotheses for
entry routing, stop construction, exit capture and regime-specific strategy
weights.  It never bypasses a hard execution/risk/source gate and never mutates
broker-facing settings.  Automatic promotion is limited to paper/shadow policy
profiles and requires the existing independent promotion gate.

The design deliberately separates:
* discovery evidence (may propose a hypothesis),
* replay/OOS/vault evidence (may move it to shadow),
* prospective shadow evidence (may promote it for paper routing), and
* live broker authority (always false here).
"""
from __future__ import annotations

from collections import Counter, defaultdict
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
import json
import math
import os
import threading

import veritas_learning_state as STORE

VERSION = "LEARNING_V2_2026_10_08"
SNAPSHOT_NAME = "learning_v2"
MAX_CANDIDATES = 64
MAX_PROFILES = 32
MIN_TRAIN = 32
VALID_DAYS = 7
DECISION_LIMIT = 1600
TRADE_LIMIT = 1200

KINDS = (
    "ENTRY_FALSE_BLOCK",
    "STOP_STRUCTURE",
    "EXIT_CAPTURE",
    "STRATEGY_WEIGHT",
)

# Directional opportunity thresholds are diagnostics, not execution floors.
MOVE = {"1m": .0019, "5m": .0019, "1h": .0040, "4h": .0060,
        "1d": .0100, "3d": .0150, "7d": .0200}

_lock = threading.RLock()
_runtime = {"version": VERSION, "updated_at": None, "profiles": [], "candidates": [],
            "status": "BUILDING", "generation": None}


def _now(value=None):
    if value is None:
        return datetime.now(timezone.utc)
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    try:
        out = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        return out.astimezone(timezone.utc) if out.tzinfo else None
    except Exception:
        return None


def _num(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
        return out if math.isfinite(out) else None
    except Exception:
        return None


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, default=str,
                      sort_keys=True, separators=(",", ":"))


def _digest(value):
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _scope(asset, horizon, regime, **extra):
    out = {"asset": str(asset or ""), "horizon": str(horizon or ""),
           "regime": str(regime or "UNKNOWN")}
    out.update({k: v for k, v in extra.items() if v not in (None, "")})
    return out


def _contract(kind, scope, proposal):
    frozen = {"version": VERSION, "kind": kind, "scope": scope, "proposal": proposal}
    return _digest(frozen)


def _hard(code):
    if not code:
        return False
    try:
        import veritas_canonical_constitution as CTC
        return str(CTC.veto_severity(str(code))).upper() == "HARD"
    except Exception:
        # Fail closed for unknown codes that look like data/risk/invalidation gates.
        text = str(code).upper()
        return any(token in text for token in (
            "SOURCE", "STALE", "INVALID", "HARD", "RISK", "STOP_MISSING",
            "TARGET_MISSING", "QUOTE", "MARKET_CLOSED", "DATA", "CONTRACT"))


def _blockers(payload):
    """Collect only explicit blocker codes; unknown codes remain hard by policy."""
    payload = payload or {}
    value = payload.get("final_gate_blockers")
    if not isinstance(value, list):
        value = []
    candidates = list(value)
    for key in ("trade_entry_reason", "paper_execution_reason", "execution_reason"):
        code = payload.get(key)
        if isinstance(code, str) and code:
            candidates.append(code)
    plan = payload.get("trade_plan") or {}
    if isinstance(plan, dict):
        gate = plan.get("entry_gate") or {}
        if isinstance(gate, dict) and isinstance(gate.get("reason"), str):
            candidates.append(gate["reason"])
    out = []
    for code in candidates:
        code = str(code)[:120]
        if code and code not in out:
            out.append(code)
    return out


def _provenance(payload):
    value = (payload or {}).get("learning_provenance")
    return value if isinstance(value, dict) else {}


def _source_key(payload):
    payload = payload or {}
    provenance = _provenance(payload)
    quote = provenance.get("quote") or {}
    identity = quote.get("source_identity") if isinstance(quote, dict) else None
    if not isinstance(identity, dict):
        context = payload.get("timeframe_entry_context") or {}
        event = context.get("event") if isinstance(context, dict) else {}
        identity = ((context.get("source_identity") if isinstance(context, dict) else None)
                    or (event.get("source_identity") if isinstance(event, dict) else None)
                    or payload.get("price_source_lock")
                    or payload.get("entry_execution_source_identity"))
    if not isinstance(identity, dict) or not identity.get("key"):
        return None
    return _digest({"key": identity.get("key"), "contract_id": identity.get("contract_id")})


def _policy_hash(payload):
    payload = payload or {}
    provenance = _provenance(payload)
    return (provenance.get("policy_hash")
            or payload.get("strategy_policy_hash")
            or ((payload.get("autonomous_learning") or {}).get("policy_hash")
                if isinstance(payload.get("autonomous_learning"), dict) else None))


def _source_verified(payload):
    provenance = _provenance(payload)
    if provenance:
        return provenance.get("eligible") is True and _source_key(payload) is not None
    return (payload or {}).get("source_gate_pass") is True and _source_key(payload) is not None


def _scenario(payload):
    payload = payload or {}
    direct = payload.get("selected_scenario")
    if direct:
        return str(direct)[:120]
    ctx = payload.get("timeframe_entry_context") or {}
    if isinstance(ctx, dict):
        return str(ctx.get("selected_scenario") or (ctx.get("event") or {}).get("event_type") or "UNKNOWN")[:120]
    return "UNKNOWN"


def _strategy_direction(payload, fallback):
    payload = payload or {}
    ctx = payload.get("timeframe_entry_context") or {}
    if isinstance(ctx, dict):
        event = ctx.get("event") or {}
        direction = event.get("direction") if isinstance(event, dict) else None
        if direction in ("LONG", "SHORT"):
            return direction
    return fallback if fallback in ("LONG", "SHORT") else None


def _signed(decision, forward_return):
    fr = _num(forward_return)
    if fr is None:
        return None
    if decision == "LONG":
        return fr
    if decision == "SHORT":
        return -fr
    return None


def _metrics(values):
    values = [float(v) for v in values if _num(v) is not None]
    if not values:
        return {"n": 0, "expectancy": None, "win_rate": None, "profit_factor": None,
                "max_drawdown": None}
    pos = sum(v for v in values if v > 0)
    neg = -sum(v for v in values if v < 0)
    equity = peak = dd = 0.0
    for value in values:
        equity += value
        peak = max(peak, equity)
        dd = max(dd, peak - equity)
    return {"n": len(values), "expectancy": sum(values)/len(values),
            "win_rate": sum(v > 0 for v in values)/len(values),
            "profit_factor": pos/neg if neg > 1e-12 else (999.0 if pos > 0 else None),
            "max_drawdown": dd}


def _split(rows):
    rows = sorted(rows, key=lambda x: (str(x.get("at") or ""), str(x.get("id") or "")))
    n = len(rows)
    if n < 10:
        return rows, [], []
    a = max(1, int(n * .55))
    b = max(a + 1, int(n * .85))
    return rows[:a], rows[a:b], rows[b:]


def _ece(rows, bins=5):
    """Expected calibration error for the frozen route probability when present."""
    buckets = [[] for _ in range(bins)]
    for row in rows:
        p = _num(row.get("probability"))
        y = row.get("correct")
        if p is None or not 0 <= p <= 1 or y not in (0, 1, False, True):
            continue
        buckets[min(bins - 1, int(p * bins))].append((p, 1.0 if y else 0.0))
    n = sum(len(b) for b in buckets)
    if not n:
        return None, 0
    value = 0.0
    for bucket in buckets:
        if not bucket:
            continue
        confidence = sum(x[0] for x in bucket) / len(bucket)
        accuracy = sum(x[1] for x in bucket) / len(bucket)
        value += len(bucket) / n * abs(confidence - accuracy)
    return value, n


def _candidate_observations(candidate, decisions):
    """Reconstruct only the candidate's declared route from immutable decisions."""
    scope = candidate.get("scope") or {}
    kind = candidate.get("kind")
    blocker = (candidate.get("proposal") or {}).get("soft_blocker")
    out = []
    for row in decisions:
        if (row.get("asset") != scope.get("asset") or row.get("horizon") != scope.get("horizon")
                or (row.get("regime") or "UNKNOWN") != scope.get("regime")):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        if (_source_key(payload) != scope.get("source_key")
                or _policy_hash(payload) != scope.get("policy_hash")):
            continue
        scenario = _scenario(payload)
        if scope.get("scenario") not in (None, scenario):
            continue
        if kind == "ENTRY_FALSE_BLOCK" and blocker not in _blockers(payload):
            continue
        direction = scope.get("direction") if kind == "ENTRY_FALSE_BLOCK" else _strategy_direction(payload, row.get("decision"))
        if direction not in ("LONG", "SHORT"):
            continue
        fr = _num(row.get("forward_return"))
        if fr is None:
            continue
        signed = fr if direction == "LONG" else -fr
        cost = _num(payload.get("modeled_round_trip_cost_pct"))
        if cost is None:
            gate = payload.get("final_economics_gate") or {}
            cost = _num(gate.get("modeled_round_trip_cost_pct")) if isinstance(gate, dict) else None
        cost = max(0.0, cost or .0016)
        p = _num(payload.get("calibrated_probability"))
        if p is None:
            p = _num(payload.get("probability"))
        if p is None:
            p = _num(payload.get("confidence"))
        if p is not None and not 0 <= p <= 1:
            p = None
        out.append({"id": row.get("entity_key"), "at": row.get("decision_ts"),
                    "signed": signed, "net": signed - cost,
                    "high_cost_net": signed - max(cost, .0040),
                    "correct": signed > 0, "probability": p,
                    "source_key": _source_key(payload), "policy_hash": _policy_hash(payload),
                    "source_ok": _source_verified(payload)})
    return sorted(out, key=lambda x: (str(x.get("at") or ""), str(x.get("id") or "")))


def _enrich_evidence(candidate, prior, decisions):
    """Attach chronological OOS/vault and *future-only* shadow evidence.

    A first registration has zero shadow observations by construction. On later
    runs only decisions strictly after the original created_at enter shadow.
    """
    rows = _candidate_observations(candidate, decisions)
    if not rows:
        return candidate
    created = _now((prior or {}).get("created_at"))
    historical = [r for r in rows if created is None or _now(r.get("at")) and _now(r["at"]) <= created]
    shadow = [r for r in rows if created is not None and _now(r.get("at")) and _now(r["at"]) > created]
    train, oos, vault = _split(historical)
    oos_m = _metrics([r["net"] for r in oos])
    vault_m = _metrics([r["net"] for r in vault])
    shadow_m = _metrics([r["net"] for r in shadow])
    high = _metrics([r["high_cost_net"] for r in (oos + vault)])
    ece, calibration_n = _ece(oos + vault)
    metrics = deepcopy(candidate.get("metrics") or {})
    metrics.update({"chronological_train": _metrics([r["net"] for r in train]),
                    "chronological_oos": oos_m, "chronological_vault": vault_m,
                    "prospective_shadow": shadow_m,
                    "prospective_shadow_started_at": (prior or {}).get("created_at"),
                    "calibration_ece": ece, "calibration_n": calibration_n})
    metrics["promotion_evidence"] = {
        "model_version": VERSION + ":" + candidate["candidate_id"][:12],
        "oos_n": int(oos_m["n"]), "oos_expectancy": float(oos_m["expectancy"] or 0.0),
        "oos_profit_factor": float(oos_m["profit_factor"] or 0.0),
        "vault_n": int(vault_m["n"]), "vault_expectancy": float(vault_m["expectancy"] or 0.0),
        "vault_profit_factor": float(vault_m["profit_factor"] or 0.0),
        "high_cost_expectancy": float(high["expectancy"] or 0.0),
        "calibration_n": int(calibration_n), "ece": float(ece) if ece is not None else 1.0,
        "shadow_trades": int(shadow_m["n"]), "shadow_expectancy": float(shadow_m["expectancy"] or 0.0),
        "shadow_max_drawdown": float(shadow_m["max_drawdown"] or 0.0),
        "code_ci_pass": os.getenv("VERITAS_CODE_CI_PASS", "0").lower() in ("1", "true", "yes", "on"),
        "calibration_applicable": False,
        "requires_baseline_outperformance": False,
        "baseline_oos_expectancy": None,
        "baseline_oos_profit_factor": None,
        "baseline_vault_expectancy": None,
        "baseline_vault_profit_factor": None,
        "baseline_shadow_expectancy": None,
        "data_parity_pass": bool(
            (oos or vault or shadow)
            and candidate.get("scope", {}).get("source_key")
            and candidate.get("scope", {}).get("policy_hash")
            and all(r.get("source_ok") is True for r in (oos + vault + shadow))
        ),
    }
    metrics["monitor"] = shadow_m
    candidate = deepcopy(candidate)
    candidate["metrics"] = metrics
    return candidate


def record_replay_evidence(pg_connect, candidate_id, evidence, *, now=None):
    """Attach independently produced replay/walk-forward evidence.

    The caller must provide the exact existing promotion-evidence schema. This is
    intentionally explicit: a heuristic diagnosis cannot promote stop/exit logic.
    """
    clock = _now(now) or datetime.now(timezone.utc)
    ensure_schema(pg_connect)
    with pg_connect() as c, c.transaction():
        row = c.execute("SELECT * FROM learning_v2_candidates WHERE candidate_id=%s FOR UPDATE", (candidate_id,)).fetchone()
        if not row:
            return {"status": "NOT_FOUND"}
        candidate = dict(row)
        metrics = candidate.get("metrics") or {}
        if isinstance(metrics, str):
            metrics = json.loads(metrics)
        if not isinstance(evidence, dict):
            raise ValueError("promotion evidence must be a mapping")
        import veritas_promotion as PROMO
        required = set(PROMO.PromotionEvidence.__annotations__)
        if set(evidence) != required:
            raise ValueError("promotion evidence fields do not match canonical gate")
        if evidence.get("requires_baseline_outperformance") is not True:
            raise ValueError("learning v2 replay promotion requires explicit baseline outperformance")
        metrics["promotion_evidence"] = deepcopy(evidence)
        candidate["metrics"] = metrics
        gate = _promotion(candidate)
        state = candidate.get("state")
        if gate and gate.get("eligible_for_production"):
            state = "PROMOTED_PAPER"
            valid_until = clock + timedelta(days=VALID_DAYS)
            c.execute("UPDATE learning_v2_candidates SET metrics=%s::jsonb,state=%s,promoted_at=%s,valid_until=%s,updated_at=%s,reason=%s WHERE candidate_id=%s",
                      (_json(metrics), state, clock, valid_until, clock, "INDEPENDENT_REPLAY_PROMOTION_GATE_PASS", candidate_id))
            _event(c, candidate_id, "PROMOTED_PAPER", {"gate": gate})
        else:
            c.execute("UPDATE learning_v2_candidates SET metrics=%s::jsonb,updated_at=%s,reason=%s WHERE candidate_id=%s",
                      (_json(metrics), clock, "PROMOTION_GATE_BLOCKED", candidate_id))
            _event(c, candidate_id, "PROMOTION_GATE_BLOCKED", {"gate": gate})
    sync_runtime(pg_connect)
    return {"status": state or "UPDATED", "gate": gate}


def ensure_schema(pg_connect, context=None):
    STORE.ensure_schema(pg_connect)
    if context:
        context.check()
    with pg_connect() as c, c.transaction():
        if context:
            c.execute("SELECT set_config('statement_timeout',%s,true)",
                      (str(max(1, min(2000, int(context.sql_timeout_ms)))),))
            c.execute("SET LOCAL lock_timeout='250ms'")
        c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_candidates(
          candidate_id TEXT PRIMARY KEY,
          kind TEXT NOT NULL,
          scope JSONB NOT NULL,
          proposal JSONB NOT NULL,
          state TEXT NOT NULL,
          created_at TIMESTAMPTZ NOT NULL,
          updated_at TIMESTAMPTZ NOT NULL,
          train_cutoff TIMESTAMPTZ,
          metrics JSONB NOT NULL DEFAULT '{}'::jsonb,
          contract_hash TEXT NOT NULL,
          valid_until TIMESTAMPTZ,
          promoted_at TIMESTAMPTZ,
          revoked_at TIMESTAMPTZ,
          reason TEXT,
          CHECK(octet_length(scope::text)<=8192),
          CHECK(octet_length(proposal::text)<=16384),
          CHECK(octet_length(metrics::text)<=65536)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS learning_v2_candidate_state ON learning_v2_candidates(state,updated_at DESC)")
        c.execute("CREATE INDEX IF NOT EXISTS learning_v2_candidate_kind ON learning_v2_candidates(kind,updated_at DESC)")
        c.execute("""CREATE TABLE IF NOT EXISTS learning_v2_events(
          id BIGSERIAL PRIMARY KEY,
          candidate_id TEXT,
          event_at TIMESTAMPTZ NOT NULL DEFAULT now(),
          event_type TEXT NOT NULL,
          payload JSONB NOT NULL DEFAULT '{}'::jsonb,
          CHECK(octet_length(payload::text)<=32768)
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS learning_v2_events_candidate ON learning_v2_events(candidate_id,event_at DESC)")
    return True


def _event(c, candidate_id, event_type, payload=None):
    c.execute("INSERT INTO learning_v2_events(candidate_id,event_type,payload) VALUES(%s,%s,%s::jsonb)",
              (candidate_id, event_type, _json(payload or {})))


def _candidate(kind, scope, proposal, training, now):
    contract = _contract(kind, scope, proposal)
    # Stable ID: prospective observations must update the same frozen contract.
    cid = contract
    return {"candidate_id": cid, "kind": kind, "scope": scope, "proposal": proposal,
            "state": training.get("state", "COLLECTING"), "created_at": now.isoformat(),
            "updated_at": now.isoformat(), "train_cutoff": training.get("train_cutoff"),
            "metrics": training, "contract_hash": contract, "valid_until": None,
            "promoted_at": None, "revoked_at": None,
            "reason": training.get("reason")}


def _entry_candidates(decisions, now):
    groups = defaultdict(list)
    for row in decisions:
        if str(row.get("decision") or "") != "NO_TRADE":
            continue
        fr = _num(row.get("forward_return"))
        if fr is None or abs(fr) < MOVE.get(str(row.get("horizon")), .004):
            continue
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        blockers = _blockers(payload)
        soft = [b for b in blockers if not _hard(b)]
        if not soft or len(soft) != len(blockers):
            continue
        direction = _strategy_direction(payload, None)
        source_key, policy_hash = _source_key(payload), _policy_hash(payload)
        if direction not in ("LONG", "SHORT") or not source_key or not policy_hash:
            continue
        correct = (direction == "LONG" and fr > 0) or (direction == "SHORT" and fr < 0)
        if not correct:
            continue
        for blocker in soft:
            key = (row["asset"], row["horizon"], row["regime"], _scenario(payload),
                   blocker, direction, source_key, policy_hash)
            groups[key].append(row)
    out = []
    for key, rows in groups.items():
        asset, horizon, regime, scenario, blocker, direction, source_key, policy_hash = key
        if len(rows) < MIN_TRAIN:
            continue
        abs_moves = [abs(float(r["forward_return"])) for r in rows]
        scope = _scope(asset, horizon, regime, scenario=scenario, direction=direction,
                       source_key=source_key, policy_hash=policy_hash)
        proposal = {"mode": "SOFT_ENTRY_RESEARCH", "soft_blocker": blocker,
                    "routing_bonus": .10, "never_override_hard_gate": True,
                    "never_override_final_economics": True, "paper_only": True}
        training = {"n": len(rows), "mean_abs_missed_move": sum(abs_moves)/len(abs_moves),
                    "blocker": blocker, "evidence_hash": _digest([r["entity_key"] for r in rows]),
                    "train_cutoff": max(str(r["decision_ts"]) for r in rows),
                    "state": "REPLAY_REQUIRED", "reason": "REPEATED_VERIFIED_FALSE_BLOCK_PATTERN"}
        out.append(_candidate("ENTRY_FALSE_BLOCK", scope, proposal, training, now))
    return out


def _strategy_candidates(decisions, now):
    groups = defaultdict(list)
    for row in decisions:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        scenario = _scenario(payload)
        decision = str(row.get("decision") or "")
        value = _signed(decision, row.get("forward_return"))
        source_key, policy_hash = _source_key(payload), _policy_hash(payload)
        if scenario == "UNKNOWN" or value is None or not source_key or not policy_hash:
            continue
        groups[(row["asset"], row["horizon"], row["regime"], scenario,
                source_key, policy_hash)].append(
            {"id": row["entity_key"], "at": row["decision_ts"], "value": value})
    out = []
    for key, rows in groups.items():
        if len(rows) < MIN_TRAIN:
            continue
        train, oos, vault = _split(rows)
        mt, mo, mv = (_metrics([r["value"] for r in part]) for part in (train, oos, vault))
        if mt["expectancy"] is None or mt["expectancy"] <= 0:
            continue
        asset, horizon, regime, scenario, source_key, policy_hash = key
        strength = min(.15, max(.02, mt["expectancy"] * 10.0))
        scope = _scope(asset, horizon, regime, scenario=scenario,
                       source_key=source_key, policy_hash=policy_hash)
        proposal = {"mode": "SCENARIO_TIEBREAK", "weight_multiplier": round(1.0 + strength, 4),
                    "paper_only": True, "never_change_direction": True,
                    "never_override_hard_gate": True}
        training = {"n": len(rows), "train": mt, "oos": mo, "vault": mv,
                    "evidence_hash": _digest([r["id"] for r in rows]),
                    "train_cutoff": max(str(r["at"]) for r in train) if train else None,
                    "state": "SHADOW" if mo["n"] >= 20 and mv["n"] >= 10 and
                              (mo["expectancy"] or -1) > 0 and (mv["expectancy"] or -1) > 0 else "REPLAY_REQUIRED",
                    "reason": "REGIME_SCENARIO_EDGE"}
        out.append(_candidate("STRATEGY_WEIGHT", scope, proposal, training, now))
    return out


def _trade_candidates(trades, now):
    grouped = defaultdict(list)
    for row in trades:
        payload = row.get("payload") if isinstance(row.get("payload"), dict) else {}
        source_key, policy_hash = _source_key(payload), _policy_hash(payload)
        if not source_key or not policy_hash:
            continue
        grouped[(row["asset"], row.get("horizon") or "", row.get("regime") or "UNKNOWN",
                 source_key, policy_hash)].append(row)
    out = []
    for (asset, horizon, regime, source_key, policy_hash), rows in grouped.items():
        if len(rows) < MIN_TRAIN:
            continue
        stop_rows = []
        exit_rows = []
        for row in rows:
            attrs = row.get("attributions") or []
            if not isinstance(attrs, list):
                attrs = []
            primary = str(row.get("primary_attribution") or "")
            if primary == "PROVEN_STOP_OR_ATR_RULE_VIOLATION" or any(
                    str(x).startswith(("INITIAL_STOP_", "EVENT_STOP_", "INITIAL_RISK_")) for x in attrs):
                stop_rows.append(row)
            capture = _num(row.get("capture_ratio"))
            mfe = _num(row.get("mfe_pct"))
            if ("PROFIT_CAPTURE_HYPOTHESIS" in attrs or
                    (capture is not None and mfe is not None and mfe > 0 and capture < .35)):
                exit_rows.append(row)
        if len(stop_rows) >= max(8, MIN_TRAIN//2):
            scope = _scope(asset, horizon, regime, source_key=source_key, policy_hash=policy_hash)
            proposal = {"mode": "WIDER_STRUCTURAL_STOP_WITH_RISK_NEUTRAL_SIZE",
                        "stop_buffer_atr_delta": .05, "max_delta": .20,
                        "risk_neutral_position_resize": True, "paper_only": True,
                        "requires_path_replay": True}
            training = {"n": len(stop_rows), "evidence_hash": _digest([r["trade_id"] for r in stop_rows]),
                        "train_cutoff": max(str(r["closed_at"]) for r in stop_rows),
                        "state": "REPLAY_REQUIRED", "reason": "PROVEN_STOP_OR_ATR_RULE_VIOLATIONS"}
            out.append(_candidate("STOP_STRUCTURE", scope, proposal, training, now))
        if len(exit_rows) >= max(8, MIN_TRAIN//2):
            captures = [_num(r.get("capture_ratio")) for r in exit_rows]
            captures = [x for x in captures if x is not None]
            scope = _scope(asset, horizon, regime, source_key=source_key, policy_hash=policy_hash)
            proposal = {"mode": "PARTIAL_1R_PLUS_STRUCTURAL_TARGET",
                        "partial_fraction": .25, "runner_fraction": .75,
                        "partial_trigger_r": 1.0,
                        "runner_exit": "ORIGINAL_STRUCTURAL_TARGET_OR_STOP_OR_HORIZON",
                        "paper_only": True, "requires_path_replay": True}
            training = {"n": len(exit_rows), "mean_capture": sum(captures)/len(captures) if captures else None,
                        "evidence_hash": _digest([r["trade_id"] for r in exit_rows]),
                        "train_cutoff": max(str(r["closed_at"]) for r in exit_rows),
                        "state": "REPLAY_REQUIRED", "reason": "REPEATED_LOW_CAPTURE_WITH_VERIFIED_MFE"}
            out.append(_candidate("EXIT_CAPTURE", scope, proposal, training, now))
    return out


def _promotion(candidate):
    metrics = candidate.get("metrics") or {}
    evidence = metrics.get("promotion_evidence")
    if not isinstance(evidence, dict):
        return None
    try:
        import veritas_promotion as PROMO
        fields = PROMO.PromotionEvidence.__annotations__
        if any(name not in evidence for name in fields):
            return None
        obj = PROMO.PromotionEvidence(**{name: evidence[name] for name in fields})
        return PROMO.promotion_gate(obj)
    except Exception as exc:
        return {"eligible": False, "blockers": ["PROMOTION_EVIDENCE_INVALID"],
                "error": type(exc).__name__}


def _merge_candidate(existing, fresh, now):
    if not existing:
        return fresh, "REGISTERED"
    # Preserve prospective lifecycle. Training may refresh diagnostics, but a
    # promoted/revoked candidate cannot be silently recreated as new evidence.
    out = deepcopy(existing)
    out["updated_at"] = now.isoformat()
    if existing.get("contract_hash") != fresh.get("contract_hash"):
        if existing.get("state") == "PROMOTED_PAPER":
            out.update(state="REVOKED", revoked_at=now.isoformat(), valid_until=None,
                       reason="CANDIDATE_CONTRACT_CHANGED")
            return out, "REVOKED"
        return fresh, "REPLACED"
    out["metrics"] = fresh["metrics"]
    if out.get("state") not in ("PROMOTED_PAPER", "REVOKED", "REJECTED"):
        out["state"] = fresh["state"]
        out["reason"] = fresh.get("reason")
    gate = _promotion(out)
    if gate and gate.get("eligible_for_production") and out.get("state") not in ("PROMOTED_PAPER", "REVOKED", "REJECTED"):
        # Historical/directional evidence may justify Shadow, but Learning 2.0
        # requires exact counterfactual replay versus the current baseline before
        # any paper-routing profile can be promoted.
        out.update(state="SHADOW", reason="EXACT_COUNTERFACTUAL_REPLAY_REQUIRED")
    if out.get("state") == "PROMOTED_PAPER":
        valid = _now(out.get("valid_until"))
        if not valid or valid <= now:
            out.update(state="REVOKED", revoked_at=now.isoformat(), valid_until=None,
                       reason="PROMOTION_EXPIRED")
            return out, "REVOKED"
        monitor = (out.get("metrics") or {}).get("monitor") or {}
        if ((monitor.get("n") or 0) >= 30 and
                ((_num(monitor.get("expectancy")) is not None and _num(monitor.get("expectancy")) < 0) or
                 (_num(monitor.get("max_drawdown")) is not None and _num(monitor.get("max_drawdown")) > .10))):
            out.update(state="REVOKED", revoked_at=now.isoformat(), valid_until=None,
                       reason="POST_PROMOTION_DEGRADATION")
            return out, "REVOKED"
    return out, "UPDATED"


def _rows(pg_connect, context=None):
    if context:
        context.check()
    with pg_connect() as c, c.transaction():
        if context:
            c.execute("SELECT set_config('statement_timeout',%s,true)",
                      (str(max(1, min(2000, int(context.sql_timeout_ms)))),))
            c.execute("SET LOCAL lock_timeout='250ms'")
        decisions = c.execute("""WITH recent AS MATERIALIZED (
          SELECT entity_key,decision_ts,asset,horizon,regime,decision,forward_return,mfe,mae
          FROM v90_decision_episodes ORDER BY decision_ts DESC LIMIT %s
        ) SELECT r.*,d.payload FROM recent r
          JOIN ledger_events d ON d.entity_key=r.entity_key AND d.event_type='decision'
          ORDER BY r.decision_ts ASC""", (DECISION_LIMIT,)).fetchall()
        if context:
            context.check()
        trades = c.execute("""SELECT e.trade_id,e.closed_at,e.asset,e.horizon,e.regime,e.setup_family,
          e.primary_attribution,e.attributions,e.capture_ratio,e.movement_realization_ratio,
          e.mfe_pct,e.mae_pct,e.giveback_pct,e.net_pnl_rub,e.expected_move_pct,e.expected_to_stop_ratio,
          (COALESCE(e.payload,'{}'::jsonb) || jsonb_build_object(
             'price_source_lock',t.payload->'price_source_lock',
             'entry_execution_source_identity',t.payload->'entry_execution_source_identity',
             'strategy_policy_hash',t.payload->'strategy_policy_hash',
             'strategy_policy_hash_version',t.payload->'strategy_policy_hash_version',
             'entry_event_snapshot',t.payload->'entry_event_snapshot',
             'initial_stop_price',t.payload->'initial_stop_price',
             'entry_execution_model',t.payload->'entry_execution_model',
             'observation_path',t.payload->'observation_path')) AS payload
          FROM v90_learning_episodes e
          JOIN paper_trades t ON t.trade_id=e.trade_id
          WHERE e.learning_eligible=TRUE
          ORDER BY e.closed_at DESC LIMIT %s""", (TRADE_LIMIT,)).fetchall()
    return [dict(r) for r in decisions], [dict(r) for r in trades]


def analyze(pg_connect, *, context=None, now=None):
    """Generate/update bounded candidates and publish a compact runtime snapshot."""
    clock = _now(now) or datetime.now(timezone.utc)
    ensure_schema(pg_connect, context=context)
    decisions, trades = _rows(pg_connect, context=context)
    fresh = (_entry_candidates(decisions, clock) + _strategy_candidates(decisions, clock)
             + _trade_candidates(trades, clock))
    # Keep strongest candidates per contract family before any write.
    fresh.sort(key=lambda c: ((c.get("metrics") or {}).get("n") or 0,
                              c.get("kind") == "STRATEGY_WEIGHT"), reverse=True)
    fresh = fresh[:MAX_CANDIDATES]
    changed = Counter()
    with pg_connect() as c, c.transaction():
        existing_rows = c.execute("SELECT * FROM learning_v2_candidates").fetchall()
        existing = {r["candidate_id"]: dict(r) for r in existing_rows}
        for candidate in fresh:
            prior = existing.get(candidate["candidate_id"])
            candidate = _enrich_evidence(candidate, prior, decisions)
            merged, event = _merge_candidate(prior, candidate, clock)
            c.execute("""INSERT INTO learning_v2_candidates(candidate_id,kind,scope,proposal,state,
              created_at,updated_at,train_cutoff,metrics,contract_hash,valid_until,promoted_at,revoked_at,reason)
              VALUES(%s,%s,%s::jsonb,%s::jsonb,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s,%s)
              ON CONFLICT(candidate_id) DO UPDATE SET state=EXCLUDED.state,updated_at=EXCLUDED.updated_at,
                metrics=EXCLUDED.metrics,valid_until=EXCLUDED.valid_until,promoted_at=EXCLUDED.promoted_at,
                revoked_at=EXCLUDED.revoked_at,reason=EXCLUDED.reason""",
              (merged["candidate_id"], merged["kind"], _json(merged["scope"]), _json(merged["proposal"]),
               merged["state"], merged["created_at"], merged["updated_at"], merged.get("train_cutoff"),
               _json(merged.get("metrics") or {}), merged["contract_hash"], merged.get("valid_until"),
               merged.get("promoted_at"), merged.get("revoked_at"), merged.get("reason")))
            if event != "UPDATED":
                _event(c, merged["candidate_id"], event, {"state": merged["state"], "reason": merged.get("reason")})
            changed[event] += 1
        # Expire active profiles even when no fresh training row was produced.
        promoted = c.execute("SELECT * FROM learning_v2_candidates WHERE state='PROMOTED_PAPER'").fetchall()
        for raw in promoted:
            row = dict(raw)
            valid = _now(row.get("valid_until"))
            if not valid or valid <= clock:
                c.execute("UPDATE learning_v2_candidates SET state='REVOKED',revoked_at=%s,valid_until=NULL,reason='PROMOTION_EXPIRED',updated_at=%s WHERE candidate_id=%s",
                          (clock, clock, row["candidate_id"]))
                _event(c, row["candidate_id"], "REVOKED", {"reason": "PROMOTION_EXPIRED"})
                changed["REVOKED"] += 1
        rows = c.execute("SELECT * FROM learning_v2_candidates ORDER BY updated_at DESC LIMIT %s", (MAX_CANDIDATES,)).fetchall()
    snapshot = _snapshot_rows([dict(r) for r in rows], clock)
    STORE.publish_snapshot(pg_connect, SNAPSHOT_NAME, VERSION, snapshot, observed_at=clock)
    update_runtime(snapshot)
    return {"status": "OK", "candidates": len(snapshot["candidates"]),
            "profiles": len(snapshot["profiles"]), "changes": dict(changed),
            "decision_rows": len(decisions), "trade_rows": len(trades)}


def _snapshot_rows(rows, now):
    candidates = []
    profiles = []
    counts = Counter()
    for raw in rows[:MAX_CANDIDATES]:
        row = dict(raw)
        for field in ("scope", "proposal", "metrics"):
            if isinstance(row.get(field), str):
                try:
                    row[field] = json.loads(row[field])
                except Exception:
                    row[field] = {}
        compact = {k: row.get(k) for k in (
            "candidate_id", "kind", "scope", "proposal", "state", "created_at", "updated_at",
            "train_cutoff", "metrics", "contract_hash", "valid_until", "promoted_at", "revoked_at", "reason")}
        counts[str(compact["state"])] += 1
        candidates.append(compact)
        valid = _now(compact.get("valid_until"))
        if compact["state"] == "PROMOTED_PAPER" and valid and valid > now:
            profiles.append(compact)
    profiles = profiles[:MAX_PROFILES]
    return {"version": VERSION, "status": "ACTIVE" if profiles else "BUILDING",
            "updated_at": now.isoformat(), "automatic_hypothesis_generation": True,
            "automatic_paper_promotion": True, "real_orders_enabled": False,
            "hard_gate_override": False, "candidate_counts": dict(counts),
            "candidates": candidates, "profiles": profiles,
            "principle": "Discovery may propose; replay/OOS/vault/shadow evidence must promote; hard gates never yield."}


def snapshot(pg_connect=None, *, now=None):
    clock = _now(now) or datetime.now(timezone.utc)
    if pg_connect is None:
        with _lock:
            return deepcopy(_runtime)
    value = STORE.load_snapshot(pg_connect, SNAPSHOT_NAME, VERSION)
    if value:
        result = deepcopy(value["payload"])
        # Filter expired profiles at read time without mutating history.
        result["profiles"] = [p for p in result.get("profiles", [])
                              if _now(p.get("valid_until")) and _now(p["valid_until"]) > clock]
        return result
    return {"version": VERSION, "status": "BUILDING", "updated_at": None,
            "candidate_counts": {}, "candidates": [], "profiles": [],
            "automatic_hypothesis_generation": True, "automatic_paper_promotion": True,
            "promotion_authority": "INDEPENDENT_REPLAY_BASELINE_GATE_ONLY",
            "real_orders_enabled": False, "hard_gate_override": False}


def update_runtime(value):
    clean = deepcopy(value or {})
    profiles = clean.get("profiles") or []
    candidates = clean.get("candidates") or []
    if len(profiles) > MAX_PROFILES or len(candidates) > MAX_CANDIDATES:
        raise ValueError("learning v2 runtime capacity exceeded")
    with _lock:
        _runtime.clear()
        _runtime.update(clean)
    return True


def sync_runtime(pg_connect):
    value = snapshot(pg_connect)
    update_runtime(value)
    return {"status": "OK", "profiles": len(value.get("profiles") or []),
            "candidates": len(value.get("candidates") or []), "updated_at": value.get("updated_at")}


def routing_context(raw):
    """Pure current source/policy identity for no-I/O decision-path matching."""
    raw = raw or {}
    identity = raw.get("structure_source_identity")
    if not isinstance(identity, dict) or not identity.get("key"):
        try:
            import veritas_price_source as SOURCE
            identity = SOURCE.identity(str(raw.get("asset") or ""), raw)
        except Exception:
            identity = None
    source_key = (_digest({"key": identity.get("key"), "contract_id": identity.get("contract_id")})
                  if isinstance(identity, dict) and identity.get("key") else None)
    try:
        import veritas_entry_version as ENTRY
        policy_hash = ENTRY.policy_hash()
    except Exception:
        policy_hash = None
    return {"source_key": source_key, "policy_hash": policy_hash}


def _matching_profiles(asset, horizon, regime, scenario=None, source_key=None, policy_hash=None):
    now = datetime.now(timezone.utc)
    with _lock:
        profiles = deepcopy(_runtime.get("profiles") or [])
    out = []
    for p in profiles:
        scope = p.get("scope") or {}
        valid = _now(p.get("valid_until"))
        if not valid or valid <= now:
            continue
        if (scope.get("asset") != asset or scope.get("horizon") != horizon
                or scope.get("regime") != (regime or "UNKNOWN")):
            continue
        if scenario is not None and scope.get("scenario") not in (None, scenario):
            continue
        if scope.get("source_key") and scope.get("source_key") != source_key:
            continue
        if scope.get("policy_hash") and scope.get("policy_hash") != policy_hash:
            continue
        out.append(p)
    return out


def scenario_weight(asset, horizon, regime, scenario, *, source_key=None, policy_hash=None):
    """Pure no-I/O routing overlay. It is only a tiebreaker among safe candidates."""
    weight = 1.0
    ids = []
    for p in _matching_profiles(asset, horizon, regime, scenario, source_key, policy_hash):
        if p.get("kind") not in ("STRATEGY_WEIGHT", "ENTRY_FALSE_BLOCK"):
            continue
        proposal = p.get("proposal") or {}
        value = _num(proposal.get("weight_multiplier"))
        if value is None and p.get("kind") == "ENTRY_FALSE_BLOCK":
            bonus = _num(proposal.get("routing_bonus"))
            value = 1.0 + bonus if bonus is not None else None
        if value is None or not .85 <= value <= 1.15:
            continue
        weight *= value
        ids.append(p.get("candidate_id"))
    return {"version": VERSION, "weight": max(.75, min(1.25, weight)),
            "candidate_ids": [x for x in ids if x][:8], "paper_only": True,
            "hard_gate_override": False}


def shadow_advice(asset, horizon, regime, scenario, blockers=None, *, source_key=None, policy_hash=None):
    """Expose applicable unpromoted hypotheses for prospective stamping only."""
    with _lock:
        candidates = deepcopy(_runtime.get("candidates") or [])
    ids = []
    blocker_set = set(str(x) for x in (blockers or []))
    for c in candidates:
        if c.get("state") not in ("REPLAY_REQUIRED", "SHADOW"):
            continue
        scope = c.get("scope") or {}
        if (scope.get("asset") != asset or scope.get("horizon") != horizon
                or scope.get("regime") != (regime or "UNKNOWN")):
            continue
        if scope.get("scenario") not in (None, scenario):
            continue
        if scope.get("source_key") and scope.get("source_key") != source_key:
            continue
        if scope.get("policy_hash") and scope.get("policy_hash") != policy_hash:
            continue
        if c.get("kind") == "ENTRY_FALSE_BLOCK":
            blocker = (c.get("proposal") or {}).get("soft_blocker")
            if blocker and blocker not in blocker_set:
                continue
        ids.append(c.get("candidate_id"))
    return {"version": VERSION, "candidate_ids": [x for x in ids if x][:8],
            "decision_influence": False, "paper_only": True}
