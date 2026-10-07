"""Bounded, prospective learning of declared adjustments, never generated rules.

Direction calibration is not evidence of profitable execution. The optional
size experiment uses observed paper cash flows, not causal real-money results.
Durable deduplication and snapshot updates share one transaction. No imports
from the live trading runtime, no network, and no broker operations occur here.
"""
from __future__ import annotations

from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import islice
import json
import math
from statistics import NormalDist

VERSION = "AUTONOMOUS_LEARNING_V1"
SNAPSHOT_NAME = "autonomous_learning"
MAX_BATCH = 256
MAX_SCOPES = 128
MAX_CANDIDATES = 24
MIN_TRAIN = 32
LOOKS = (64, 128, 256)
MIN_DAYS = 14
MAX_DAYS = 32
VALID_DAYS = 7
TERMINAL = frozenset(("rejected", "degraded"))
KINDS = ("CALIBRATION", "SIZE_DOWN_WEAK_SIGNAL")
CONTRACT_FIELDS = ("version", "scope", "kind", "proposal", "created_at", "train_cutoff",
                   "training_n", "training_evidence_hash", "training_epoch", "criteria")
CRITERIA = {"training_ideas": MIN_TRAIN, "prospective_looks": LOOKS,
            "minimum_utc_days": MIN_DAYS, "block_interval_multiplier": 3.5,
            "calibration_offsets": [-.05, .05], "size_multiplier": .9,
            "weak_probability_threshold": .60, "evidence_valid_days": VALID_DAYS,
            "profitability_required_for_size": True,
            "family_alpha_budget": .05,
            "independence": "VERIFIED_IDEA_AND_NONOVERLAPPING_ASSET_INTERVALS",
            "inference": "FIXED_LOOK_DAILY_BLOCK_APPROXIMATION_NOT_A_GUARANTEE"}


def _digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     default=str, allow_nan=False).encode()).hexdigest()


def _time(value):
    if isinstance(value, datetime):
        return value.astimezone(timezone.utc) if value.tzinfo else None
    if not isinstance(value, str):
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result.astimezone(timezone.utc) if result.tzinfo else None
    except ValueError:
        return None


def _clock(value=None):
    result = _time(value) if value is not None else datetime.now(timezone.utc)
    if result is None:
        raise ValueError("timezone-aware clock required")
    return result


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _probability(value):
    result = _number(value)
    return result if result is not None and 0 <= result <= 1 else None


def _iso(value):
    return _clock(value).isoformat()


def scope_for(row):
    """Keep instrument/source, policy, regime and horizon cohorts separate."""
    return {"asset": str(row.get("asset") or ""),
            "horizon": str(row.get("horizon") or ""),
            "regime": str(row.get("regime") or "UNKNOWN"),
            "policy_hash": str(row.get("policy_hash") or ""),
            "source_key": _digest(row.get("source_identity")) if row.get("source_identity") else ""}


def observation_key(row):
    # Exclude timeframe/portfolio/direction: several reports of one market idea
    # are not several observations. Financial evidence is a separate channel.
    idea = row.get("idea_id") or row.get("event_id")
    if not idea or not row.get("asset"):
        return None
    channel = "trade" if row.get("proof_kind") else "direction"
    return _digest([str(row["asset"]), str(idea), channel])


def normalize_observation(row, now=None):
    """Return a small validated record or an explicit exclusion; never fill 0."""
    clock = _clock(now)
    decision = _time(row.get("decision_at") or row.get("event_ts"))
    outcome = _time(row.get("outcome_at") or row.get("outcome_ts"))
    known = _time(row.get("known_at"))
    observed = _time(row.get("observed_at"))
    reason = None
    if observation_key(row) is None:
        reason = "MISSING_MARKET_IDEA"
    elif row.get("evidence_valid") is not True:
        reason = "EVIDENCE_INVALID_OR_UNVERIFIED"
    elif row.get("source_verified") is not True or not row.get("source_identity"):
        reason = "SOURCE_UNVERIFIED"
    elif row.get("independence_verified") is not True:
        reason = "MARKET_IDEA_UNVERIFIED"
    elif not row.get("evidence_hash") or not row.get("evidence_version"):
        reason = "MISSING_EVIDENCE_IDENTITY"
    elif not all((decision, outcome, known, observed)):
        reason = "MISSING_OBSERVED_TIMES"
    elif not known <= decision < outcome <= observed <= clock:
        reason = "LOOKAHEAD_OR_INCOMPLETE_OUTCOME"
    elif not row.get("asset") or not row.get("horizon") or row.get("direction") not in ("LONG", "SHORT"):
        reason = "INVALID_DIRECTION_SCOPE"
    forward = _number(row.get("forward_return"))
    if reason is None and forward is None and not row.get("proof_kind"):
        reason = "MISSING_DIRECTION_OUTCOME"
    if reason:
        return None, reason
    keep = ("episode_key", "idea_id", "event_id", "asset", "horizon", "regime", "direction",
            "policy_hash", "evidence_hash", "evidence_version", "source_identity", "proof_kind",
            "candidate_id", "candidate_decision_at", "costs_verified", "risk_verified",
            "baseline_net_r", "candidate_net_r", "baseline_risk_r", "candidate_risk_r",
            "risk_cap_r", "net_r", "source_verified", "evidence_valid", "independence_verified", "size_execution")
    result = {key: row.get(key) for key in keep}
    result.update(key=observation_key(row), scope=scope_for(row),
                  decision_at=decision.isoformat(), outcome_at=outcome.isoformat(),
                  observed_at=observed.isoformat(), known_at=known.isoformat(),
                  predicted_probability=_probability(row.get("predicted_probability")),
                  forward_return=forward, channel="trade" if row.get("proof_kind") else "direction")
    result["direction_correct"] = (float((forward if row["direction"] == "LONG" else -forward) > 0)
                                   if forward is not None else None)
    return result, None


def _stats():
    return {"n": 0, "days": {}, "sum_base": 0., "sum_candidate": 0.,
            "sum_delta": 0., "base_equity": 0., "candidate_equity": 0.,
            "base_peak": 0., "candidate_peak": 0., "base_drawdown": 0.,
            "candidate_drawdown": 0., "wins": 0}


def _add(stats, day, base, candidate, delta, win):
    block = stats["days"].setdefault(day, [0, 0., 0., 0.])
    block[0] += 1
    block[1] += delta
    block[2] += base
    block[3] += candidate
    stats["n"] += 1
    stats["wins"] += int(win)
    stats["sum_base"] += base
    stats["sum_candidate"] += candidate
    stats["sum_delta"] += delta
    for label, value in (("base", base), ("candidate", candidate)):
        stats[label+"_equity"] += value
        stats[label+"_peak"] = max(stats[label+"_peak"], stats[label+"_equity"])
        stats[label+"_drawdown"] = max(stats[label+"_drawdown"],
                                       stats[label+"_peak"]-stats[label+"_equity"])


def _interval(values, multiplier=3.5):
    """Conservative fixed-look descriptive interval over UTC day means."""
    n = len(values)
    if n < 2:
        return [None, None]
    mean = sum(values)/n
    variance = sum((value-mean)**2 for value in values)/(n-1)
    half = multiplier*math.sqrt(variance/n)
    return [mean-half, mean+half]


def _summary(stats, criteria=None):
    n = stats["n"]
    criteria = criteria or CRITERIA
    multiplier = criteria["block_interval_multiplier"]
    return {"n": n, "days": len(stats["days"]),
            "mean_delta": stats["sum_delta"]/n if n else None,
            "mean_baseline": stats["sum_base"]/n if n else None,
            "mean_candidate": stats["sum_candidate"]/n if n else None,
            "delta_interval": _interval([b[1]/b[0] for b in stats["days"].values()], multiplier),
            "candidate_interval": _interval([b[3]/b[0] for b in stats["days"].values()], multiplier),
            "win_rate": stats["wins"]/n if n else None,
            "base_drawdown_r": stats["base_drawdown"],
            "candidate_drawdown_r": stats["candidate_drawdown"],
            "target_win_rate": .65, "target_win_rate_proven": False,
            "trial_index": criteria.get("trial_index"), "per_look_alpha": criteria.get("per_look_alpha"),
            "inference": CRITERIA["inference"]}


def register_candidate(scope, training, kind, now=None, *, trial_index=1):
    """Freeze the proposal and criteria before any evaluation decisions exist."""
    if kind not in KINDS or training.get("n", 0) < MIN_TRAIN or not scope.get("policy_hash"):
        raise ValueError("candidate requires eligible training and a known policy")
    clock = _clock(now)
    if type(trial_index) is not int or trial_index < 1:
        raise ValueError("positive immutable trial index required")
    criteria = deepcopy(CRITERIA)
    # The telescoping budget covers all registered trials, not merely the
    # successful ones; divide again for fixed looks and the two size tests.
    alpha = CRITERIA["family_alpha_budget"]/(trial_index*(trial_index+1)*len(LOOKS)*2)
    criteria.update(trial_index=trial_index, per_look_alpha=alpha,
                    block_interval_multiplier=max(3.5, NormalDist().inv_cdf(1-max(alpha/2, 1e-15))))
    delta = .05 if training.get("residual_sum", 0) > 0 else -.05
    proposal = ({"probability_delta": delta, "size_multiplier": 1.}
                if kind == "CALIBRATION" else
                {"probability_delta": 0., "size_multiplier": .9,
                 "condition": {"field": "base_probability", "operator": "lt", "value": .60}})
    frozen = {"version": VERSION, "scope": dict(scope), "kind": kind, "proposal": proposal,
              "created_at": clock.isoformat(), "train_cutoff": clock.isoformat(),
              "training_n": training["n"], "training_evidence_hash": training.get("evidence_hash"),
              "training_epoch": training.get("epoch", 0),
              "criteria": criteria}
    candidate = {**frozen, "candidate_id": _digest(frozen), "contract_hash": _digest(frozen),
                 "state": "collecting", "reasons": ["AWAIT_PROSPECTIVE_DECISIONS"],
                 "stats": _stats(), "monitor": _stats(), "completed_looks": [],
                 "last_outcome_at": None, "last_decision_at": None, "excluded": {},
                 "profitability_proven": False, "evidence_valid": True, "valid_until": None}
    return candidate


def _candidate_values(candidate, row):
    p = row["predicted_probability"]
    if p is None:
        return None, "MISSING_FROZEN_PROBABILITY"
    if candidate["kind"] == "CALIBRATION":
        y = row["direction_correct"]
        if y is None or row["channel"] != "direction":
            return None, "DIRECTION_OUTCOME_REQUIRED"
        adjusted = min(1., max(0., p+candidate["proposal"]["probability_delta"]))
        baseline, result = (p-y)**2, (adjusted-y)**2
        return (baseline, result, baseline-result, bool(y)), None
    if row.get("proof_kind") not in ("SIMULATED_SIZE_ON_OBSERVED_PATH", "PAIRED_OBSERVED_PAPER_PATH"):
        return None, "WAIT_VERIFIED_PROFITABILITY"
    stamp = _time(row.get("candidate_decision_at"))
    if (row.get("candidate_id") != candidate["candidate_id"] or stamp is None
            or not _time(candidate["created_at"]) < stamp <= _time(row["decision_at"])):
        return None, "MISSING_PROSPECTIVE_CANDIDATE_STAMP"
    fields = ["baseline_net_r", "candidate_net_r", "baseline_risk_r", "candidate_risk_r", "risk_cap_r"]
    values = [_number(row.get(key)) for key in fields]
    if any(value is None for value in values) or row.get("costs_verified") is not True or row.get("risk_verified") is not True:
        return None, "WAIT_VERIFIED_COSTS_AND_RISK"
    base, result, base_risk, risk, cap = values
    factor = .9 if p < .60 else 1.
    quantized = row.get("size_execution")
    if quantized is not None:
        if not isinstance(quantized, dict):
            return None, "INVALID_SIZE_EXECUTION_PROOF"
        expected = size_execution(p, fraction=quantized.get("fraction_before"),
                                  step=quantized.get("position_step"), cap=quantized.get("max_fraction"))
        if expected is None or quantized != expected:
            return None, "INVALID_SIZE_EXECUTION_PROOF"
        factor = expected["effective_multiplier"]
    if not (0 < base_risk <= cap and 0 < risk <= cap and math.isclose(risk/base_risk, factor, rel_tol=1e-6)):
        return None, "PAIRED_RISK_OR_CONDITION_MISMATCH"
    if row["proof_kind"] == "SIMULATED_SIZE_ON_OBSERVED_PATH" and not math.isclose(result, base*factor, rel_tol=1e-7, abs_tol=1e-9):
        return None, "SIMULATED_CASHFLOW_MISMATCH"
    return (base, result, result-base, result > 0), None


def evaluate(candidate, observations, *, now=None):
    """Pure incremental evaluation; source rows never enter the saved candidate."""
    out = deepcopy(candidate)
    clock = _clock(now)
    if (out.get("version") != VERSION or out.get("contract_hash") !=
            _digest({key: out.get(key) for key in CONTRACT_FIELDS})):
        out.update(state="degraded", evidence_valid=False, reasons=["CANDIDATE_CONTRACT_CHANGED"])
        return out
    if out["state"] in TERMINAL:
        return out
    if out["state"] == "promoted" and (not _time(out.get("valid_until")) or _time(out["valid_until"]) <= clock):
        out.update(state="degraded", reasons=["EVIDENCE_EXPIRED"], evidence_valid=False)
        return out
    seen = set()
    for raw in observations:
        # Internal normalized rows carry these booleans too; callers cannot
        # bypass provenance checks merely by adding a `scope` field.
        row, reason = normalize_observation(raw, clock)
        if reason:
            out["excluded"][reason] = out["excluded"].get(reason, 0)+1
            continue
        if row["scope"] != out["scope"]:
            continue
        if row["key"] in seen:
            continue
        seen.add(row["key"])
        if _time(row["decision_at"]) <= _time(out["created_at"]):
            reason = "PRE_REGISTRATION_DECISION"
        elif out.get("last_outcome_at") and _time(row["decision_at"]) < _time(out["last_outcome_at"]):
            reason = "OVERLAPPING_OR_REPLAYED_IDEA"
        else:
            values, reason = _candidate_values(out, row)
        if reason:
            out["excluded"][reason] = out["excluded"].get(reason, 0)+1
            if out["state"] != "promoted":
                out["state"] = "evaluating"
                out["reasons"] = [reason]
            continue
        stats = out["monitor"] if out["state"] == "promoted" else out["stats"]
        day = row["decision_at"][:10]
        if day not in stats["days"] and len(stats["days"]) >= MAX_DAYS:
            out.update(state="degraded" if out["state"] == "promoted" else "rejected",
                       reasons=["ASSESSMENT_WINDOW_EXHAUSTED"], evidence_valid=False)
            break
        _add(stats, day, *values)
        out["last_outcome_at"], out["last_decision_at"] = row["outcome_at"], row["decision_at"]
        summary = _summary(stats, out["criteria"])
        summary["metric"] = "BRIER_LOSS" if out["kind"] == "CALIBRATION" else "NET_R_AFTER_COSTS"
        summary["proof_kind"] = "DIRECTION_OUTCOMES" if out["kind"] == "CALIBRATION" else row["proof_kind"]
        if out["state"] == "promoted":
            out["monitor_evidence"] = summary
            if stats["n"] >= 32 and summary["days"] >= 7:
                if summary["delta_interval"][1] < 0 or (out["kind"] != "CALIBRATION" and summary["candidate_interval"][1] < 0):
                    out.update(state="degraded", reasons=["PROSPECTIVE_DETERIORATION"], evidence_valid=False)
                    break
                # Refresh only at complete monitoring blocks; stale evidence
                # expires rather than being kept alive by a scheduler heartbeat.
                out["valid_until"] = (clock+timedelta(days=VALID_DAYS)).isoformat()
                out["monitor"] = _stats()
            continue
        out.update(state="evaluating", reasons=["INSUFFICIENT_PROSPECTIVE_SAMPLE"], evidence=summary)
        look = next((n for n in LOOKS if n not in out["completed_looks"] and stats["n"] >= n), None)
        if look is None:
            continue
        out["completed_looks"].append(look)
        if summary["days"] < MIN_DAYS:
            out["reasons"] = ["INSUFFICIENT_TEMPORAL_COVERAGE"]
        elif summary["delta_interval"][0] > 0:
            profit = out["kind"] == "CALIBRATION" or (
                summary["candidate_interval"][0] > 0 and
                summary["candidate_drawdown_r"] <= summary["base_drawdown_r"]+1e-9)
            if profit:
                out.update(state="promoted", reasons=["PROSPECTIVE_CALIBRATION_IMPROVED" if out["kind"] == "CALIBRATION" else "PROSPECTIVE_PAPER_ALLOCATION_IMPROVED"],
                           promoted_at=clock.isoformat(), valid_until=(clock+timedelta(days=VALID_DAYS)).isoformat(),
                           profitability_proven=out["kind"] != "CALIBRATION")
            else:
                out["reasons"] = ["PROFITABILITY_OR_DRAWDOWN_GATE_FAILED"]
        else:
            out["reasons"] = ["IMPROVEMENT_NOT_ESTABLISHED"]
        if look == LOOKS[-1] and out["state"] != "promoted":
            out["state"] = "rejected"
    return out


def initial_state():
    return {"version": VERSION, "status": "collecting", "updated_at": None,
            "scopes": {}, "candidates": {}, "streams": {}, "counts": {},
            "reasons": {}, "recent_events": [], "criteria": CRITERIA}


def _event(state, event, at, **data):
    state["recent_events"] = (state["recent_events"]+[{"event": event, "at": at, **data}])[-24:]


def _invalidate(state, scope_key, at, reason, evidence_epoch=0):
    scope = state["scopes"].get(scope_key)
    if scope and scope.get("epoch", 0) != evidence_epoch:
        _event(state, "ARCHIVED_EVIDENCE_INVALIDATED", at, scope_key=scope_key, evidence_epoch=evidence_epoch)
        return None
    candidate_ids = []
    for candidate in state["candidates"].values():
        if _digest(candidate["scope"]) == scope_key and candidate["state"] not in TERMINAL:
            candidate_ids.append(candidate["candidate_id"])
            candidate.update(state="degraded", evidence_valid=False, reasons=[reason], valid_until=at)
            _event(state, "REVOKED", at, candidate_id=candidate["candidate_id"], reason=reason)
    if scope:
        scope["quarantined"] = True
        scope["quarantine_at"] = at
        return {"scope_key": scope_key, "epoch": scope.get("epoch", 0), "quarantine_at": at,
                "training": deepcopy(scope), "candidate_ids": candidate_ids, "reason": reason}
    return None


def _consume(state, row, clock):
    key = _digest(row["scope"])
    if key not in state["scopes"]:
        if len(state["scopes"]) >= MAX_SCOPES:
            return "SCOPE_CAPACITY_REACHED"
        state["scopes"][key] = {"scope": row["scope"], "n": 0, "correct": 0,
                                "probability_n": 0, "residual_sum": 0., "evidence_hash": None}
    scope = state["scopes"][key]
    if scope.get("quarantined"):
        if _time(row["decision_at"]) <= _time(scope["quarantine_at"]):
            return "SCOPE_EVIDENCE_QUARANTINED"
        # Resume only on a newly formed, verified idea after revocation. None
        # of the invalidated sufficient statistics enter the replacement trial.
        scope = {"scope": row["scope"], "n": 0, "correct": 0, "probability_n": 0,
                 "residual_sum": 0., "evidence_hash": None, "epoch": scope.get("epoch", 0)+1,
                 "prior_n": scope.get("prior_n", 0)+scope["n"], "epoch_started_at": scope["quarantine_at"],
                 "registration_count": scope.get("registration_count", 0),
                 "quarantined": False}
        state["scopes"][key] = scope
        _event(state, "FRESH_EVIDENCE_EPOCH", clock.isoformat(), scope_key=key, epoch=scope["epoch"])
    stream_key = row["scope"]["asset"]+":"+row["channel"]
    last = state["streams"].get(stream_key)
    if last and _time(row["decision_at"]) < _time(last):
        return "OVERLAPPING_ASSET_IDEA"
    state["streams"][stream_key] = row["outcome_at"]
    scope["last_seen_at"] = row["observed_at"]
    if row["channel"] == "direction":
        scope["n"] += 1
        scope["correct"] += int(row["direction_correct"])
        if row["predicted_probability"] is not None:
            scope["probability_n"] += 1
            scope["residual_sum"] += row["direction_correct"]-row["predicted_probability"]
        scope["evidence_hash"] = _digest([scope["evidence_hash"], row["evidence_hash"]])
    for cid, candidate in list(state["candidates"].items()):
        if candidate["scope"] == row["scope"]:
            result = evaluate(candidate, [row], now=clock)
            state["candidates"][cid] = result
            if result["state"] != candidate["state"]:
                _event(state, result["state"].upper(), clock.isoformat(), candidate_id=cid, reasons=result["reasons"])
    return None


def _register(state, clock):
    existing = {(_digest(c["scope"]), c["kind"]) for c in state["candidates"].values()
                if c["state"] not in TERMINAL}
    ordered = sorted(state["scopes"].items(), key=lambda item: (
        item[1].get("registration_count", len(item[1].get("registered_at_n", {}))), item[0]))
    for key, training in ordered:
        if (training["probability_n"] < MIN_TRAIN or training.get("quarantined")
                or not training["scope"]["policy_hash"]):
            continue
        for kind in KINDS:
            if (key, kind) in existing or len(state["candidates"]) >= MAX_CANDIDATES:
                continue
            last = (training.get("registered_at_n") or {}).get(kind, 0)
            if training["n"]-last < MIN_TRAIN:
                continue
            trial_index = state["counts"].get("registered_trials", 0)+1
            candidate = register_candidate(training["scope"], training, kind, clock, trial_index=trial_index)
            state["candidates"][candidate["candidate_id"]] = candidate
            state["counts"]["registered_trials"] = trial_index
            training.setdefault("registered_at_n", {})[kind] = training["n"]
            training["registration_count"] = training.get("registration_count", 0)+1
            _event(state, "REGISTERED", clock.isoformat(), candidate_id=candidate["candidate_id"], kind=kind)


def ensure_schema(pg_connect):
    import veritas_learning_state as store
    store.ensure_schema(pg_connect)
    with pg_connect() as c, c.transaction():
        c.execute("SET LOCAL statement_timeout = '4000ms'")
        c.execute("""CREATE TABLE IF NOT EXISTS autonomous_learning_seen (
            idea_key TEXT PRIMARY KEY, scope_key TEXT NOT NULL,
            original_key TEXT NOT NULL, scope_epoch INTEGER NOT NULL DEFAULT 0,
            evidence_hash TEXT NOT NULL, valid BOOLEAN NOT NULL,
            observed_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS autonomous_learning_archive (
            candidate_id TEXT PRIMARY KEY, state TEXT NOT NULL,
            payload JSONB NOT NULL, archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS autonomous_learning_scope_archive (
            epoch_key TEXT PRIMARY KEY, payload JSONB NOT NULL,
            archived_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")
        c.execute("""CREATE TABLE IF NOT EXISTS autonomous_learning_cold_scopes (
            scope_key TEXT PRIMARY KEY, payload JSONB NOT NULL,
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW())""")


def _warm_scope(c, state, scope_key):
    """Move inactive cohorts to durable cold storage, without losing knowledge."""
    if scope_key in state["scopes"]:
        return True
    if len(state["scopes"]) >= MAX_SCOPES:
        active = {_digest(candidate["scope"]) for candidate in state["candidates"].values()
                  if candidate["state"] not in TERMINAL}
        choices = [(scope.get("last_seen_at", ""), key) for key, scope in state["scopes"].items() if key not in active]
        if not choices:
            return False
        _, evicted = min(choices)
        c.execute("""INSERT INTO autonomous_learning_cold_scopes(scope_key,payload) VALUES(%s,%s::jsonb)
            ON CONFLICT(scope_key) DO UPDATE SET payload=EXCLUDED.payload,updated_at=now()""",
                  (evicted, json.dumps(state["scopes"][evicted], allow_nan=False)))
        del state["scopes"][evicted]
        state["counts"]["cold_scopes"] = state["counts"].get("cold_scopes", 0)+1
    stored = c.execute("SELECT payload FROM autonomous_learning_cold_scopes WHERE scope_key=%s", (scope_key,)).fetchone()
    if stored:
        payload = stored["payload"]
        state["scopes"][scope_key] = json.loads(payload) if isinstance(payload, str) else deepcopy(payload)
        c.execute("DELETE FROM autonomous_learning_cold_scopes WHERE scope_key=%s", (scope_key,))
        state["counts"]["cold_scopes"] = max(0, state["counts"].get("cold_scopes", 0)-1)
    return True


def run_batch(pg_connect, observations=(), *, now=None, context=None):
    """Consume <=256 compact rows, atomically with durable idea deduplication.

    Caller must call ensure_schema at installation. Source progress/watermark
    should advance only after this function succeeds. Errors roll back all
    counters and are raised; a retry cannot double-count a committed idea.
    """
    import veritas_learning_state as store
    clock = _clock(now)
    rows = list(islice(observations, MAX_BATCH+1))
    if len(rows) > MAX_BATCH:
        raise ValueError("learning batch exceeds 256 observations")
    rows.sort(key=lambda r: str(r.get("decision_at") or r.get("event_ts") or ""))
    with pg_connect() as c, c.transaction():
        if context is not None:
            context.check()
        timeout = max(1, min(2000, int(getattr(context, "sql_timeout_ms", 2000))))
        c.execute("SET LOCAL statement_timeout = '"+str(timeout)+"ms'")
        prior = store.load_snapshot_in_transaction(c, SNAPSHOT_NAME, VERSION, for_update=True)
        state = deepcopy(prior["payload"]) if prior else initial_state()
        for raw in rows:
            if context is not None:
                context.check()
            key = observation_key(raw)
            row, reason = normalize_observation(raw, clock)
            if key is None:
                state["reasons"][reason] = state["reasons"].get(reason, 0)+1
                continue
            scope_key = _digest(scope_for(raw))
            evidence_hash = str(raw.get("evidence_hash") or "MISSING")
            original_key = str(raw.get("episode_key") or key)
            prior_row = c.execute("SELECT evidence_hash,scope_key,valid,original_key,scope_epoch FROM autonomous_learning_seen WHERE idea_key=%s", (key,)).fetchone()
            if prior_row:
                # Two forecasts of the same event can have different horizons
                # and outcome hashes. They are duplicate ideas, not revisions
                # of the original record that supplied the accepted evidence.
                revised = (prior_row["original_key"] == original_key and
                           (prior_row["evidence_hash"] != evidence_hash or reason is not None))
                if prior_row["valid"] and (revised or raw.get("evidence_valid") is False):
                    if not _warm_scope(c, state, prior_row["scope_key"]):
                        raise RuntimeError("cannot load evidence scope for mandatory invalidation")
                    archived = _invalidate(state, prior_row["scope_key"], clock.isoformat(), "SOURCE_EVIDENCE_INVALIDATED", prior_row["scope_epoch"])
                    if archived:
                        c.execute("INSERT INTO autonomous_learning_scope_archive(epoch_key,payload) VALUES(%s,%s::jsonb) ON CONFLICT DO NOTHING",
                                  (_digest([prior_row["scope_key"], prior_row["scope_epoch"]]), json.dumps(archived, allow_nan=False)))
                    c.execute("UPDATE autonomous_learning_seen SET valid=FALSE WHERE idea_key=%s", (key,))
                continue
            if reason is None:
                reason = (_consume(state, row, clock) if _warm_scope(c, state, scope_key)
                          else "ACTIVE_SCOPE_CAPACITY_REACHED")
            epoch = state["scopes"].get(scope_key, {}).get("epoch", 0)
            c.execute("INSERT INTO autonomous_learning_seen(idea_key,scope_key,original_key,scope_epoch,evidence_hash,valid,observed_at) VALUES(%s,%s,%s,%s,%s,%s,%s)",
                      (key, scope_key, original_key, epoch, evidence_hash, reason is None, clock))
            if reason:
                state["reasons"][reason] = state["reasons"].get(reason, 0)+1
            else:
                channel = row["channel"]
                state["counts"][channel] = state["counts"].get(channel, 0)+1
        for cid, candidate in list(state["candidates"].items()):
            state["candidates"][cid] = evaluate(candidate, [], now=clock)
        # Completed trials remain auditable in PostgreSQL without making the
        # hot snapshot or its update cost grow with all historical experiments.
        for cid, candidate in list(state["candidates"].items()):
            if len(state["candidates"]) <= MAX_CANDIDATES-4:
                break
            if candidate["state"] in TERMINAL:
                c.execute("INSERT INTO autonomous_learning_archive(candidate_id,state,payload) VALUES(%s,%s,%s::jsonb) ON CONFLICT DO NOTHING",
                          (cid, candidate["state"], json.dumps(candidate, allow_nan=False)))
                del state["candidates"][cid]
                state["counts"]["archived_trials"] = state["counts"].get("archived_trials", 0)+1
        _register(state, clock)
        state.update(updated_at=clock.isoformat(), status=_overall_state(state["candidates"].values()))
        if context is not None:
            context.check()
        saved = store.publish_snapshot_in_transaction(c, SNAPSHOT_NAME, VERSION, state, observed_at=clock)
        if not saved:
            raise RuntimeError("autonomous learning snapshot publication rejected")
    return public_snapshot(state, now=clock)


def _overall_state(candidates):
    states = {candidate["state"] for candidate in candidates}
    return next((state for state in ("promoted", "evaluating", "collecting", "degraded", "rejected") if state in states), "collecting")


def public_snapshot(state, *, now=None):
    clock = _clock(now)
    candidates = []
    profiles = []
    for original in state.get("candidates", {}).values():
        candidate = evaluate(original, [], now=clock)
        compact = {k: deepcopy(candidate.get(k)) for k in (
            "candidate_id", "version", "kind", "scope", "proposal", "created_at", "train_cutoff",
            "training_n", "state", "reasons", "evidence", "monitor_evidence", "profitability_proven",
            "evidence_valid", "valid_until", "contract_hash", "excluded", "training_evidence_hash", "training_epoch", "criteria")}
        candidates.append(compact)
        if candidate["state"] == "promoted" and candidate["evidence_valid"]:
            profiles.append(compact)
    lessons = [{"scope": s["scope"], "independent_ideas": s["n"],
                "direction_hit_rate": s["correct"]/s["n"] if s["n"] else None,
                "probability_observations": s["probability_n"],
                "mean_probability_error": s["residual_sum"]/s["probability_n"] if s["probability_n"] else None,
                "quarantined": s.get("quarantined", False), "epoch": s.get("epoch", 0),
                "prior_epoch_ideas": s.get("prior_n", 0), "profitability_proven": False}
               for s in state.get("scopes", {}).values()]
    return {"version": VERSION, "status": _overall_state(candidates),
            "updated_at": state.get("updated_at"), "counts": state.get("counts", {}),
            "hot_scope_count": len(state.get("scopes", {})),
            "count_semantics": "HISTORICALLY_CONSUMED_NOT_CURRENT_VALID_SAMPLE",
            "reasons": state.get("reasons", {}), "candidate_counts": dict(Counter(c["state"] for c in candidates)),
            "candidates": candidates, "profiles": profiles, "lessons": lessons,
            "recent_events": state.get("recent_events", []), "automatic_learning": True,
            "real_orders_enabled": False, "target_win_rate": .65,
            "target_win_rate_proven": False, "profitability_not_implied_by_calibration": True}


def snapshot(pg_connect=None, *, now=None):
    if pg_connect is None:
        return {**public_snapshot(initial_state(), now=now), "status": "unavailable", "reasons": {"PERSISTENT_STORE_UNAVAILABLE": 1}}
    import veritas_learning_state as store
    value = store.load_snapshot(pg_connect, SNAPSHOT_NAME, VERSION)
    return public_snapshot(value["payload"] if value else initial_state(), now=now)


def apply_advice(profiles, row, *, policy_hash=None, now=None):
    """Pure receiver contract. Caller still owns every admission/risk hard gate."""
    clock = _clock(now)
    records = profiles.get("profiles", []) if isinstance(profiles, dict) else profiles
    target = scope_for(dict(row, policy_hash=policy_hash if policy_hash is not None else row.get("policy_hash")))
    p = _probability(row.get("predicted_probability", row.get("base_probability")))
    out = {"status": "neutral", "size_multiplier": 1., "probability_delta": 0.,
           "calibrated_probability": p, "candidate_ids": [], "reasons": [],
           "profitability_proven": False, "version": VERSION, "policy_hash": target["policy_hash"]}
    if not target["policy_hash"] or not target["source_key"] or p is None:
        out["reasons"] = ["MISSING_POLICY_SOURCE_OR_PROBABILITY"]
        return out
    used = set()
    for candidate in records or []:
        kind = candidate.get("kind")
        if (kind not in KINDS or kind in used or candidate.get("version") != VERSION
                or candidate.get("contract_hash") != _digest({key: candidate.get(key) for key in CONTRACT_FIELDS})
                or candidate.get("scope") != target or candidate.get("state") != "promoted"
                or candidate.get("evidence_valid") is not True
                or not _time(candidate.get("valid_until")) or _time(candidate["valid_until"]) <= clock):
            continue
        proposal = candidate.get("proposal") or {}
        if kind == "CALIBRATION":
            delta = _number(proposal.get("probability_delta"))
            if delta not in (-.05, .05):
                continue
            out["calibrated_probability"] = min(1., max(0., p+delta))
            out["probability_delta"] = out["calibrated_probability"]-p
        elif candidate.get("profitability_proven") is True:
            if proposal.get("condition") != {"field": "base_probability", "operator": "lt", "value": .60} or proposal.get("size_multiplier") != .9:
                continue
            out["size_multiplier"] = .9 if p < .60 else 1.
            out["profitability_proven"] = True
        else:
            continue
        used.add(kind)
        out["candidate_ids"].append(candidate["candidate_id"])
    if out["candidate_ids"]:
        out["status"] = "applied"
    else:
        out["reasons"] = ["NO_CURRENT_PROVEN_CANDIDATE"]
    return out


status = snapshot


def size_execution(probability, *, fraction, step, cap):
    """Freeze exactly the existing 10%-bounded position-step calculation."""
    p, before, tick, maximum = (_probability(probability), _number(fraction),
                                _number(step), _number(cap))
    if p is None or any(x is None or x <= 0 for x in (before, tick, maximum)) or before > maximum:
        return None
    requested = .9 if p < .60 else 1.
    proposed = math.floor(before*requested/tick+1e-9)*tick
    after = proposed if .9*before-1e-10 <= proposed <= min(1.1*before, maximum)+1e-10 else before
    return {"fraction_before": before, "fraction_after": after, "position_step": tick,
            "max_fraction": maximum, "effective_multiplier": after/before}


def freeze_candidates(snapshot_value, row, *, now=None):
    """Bounded prospective experiment stamps; this never grants admission."""
    clock = _clock(now)
    scope = scope_for(row)
    probability = _probability(row.get("predicted_probability", row.get("base_probability")))
    result = []
    if probability is None:
        return result
    for candidate in (snapshot_value or {}).get("candidates", [])[:MAX_CANDIDATES]:
        if (candidate.get("scope") != scope or candidate.get("state") not in ("collecting", "evaluating", "promoted")
                or candidate.get("evidence_valid") is not True or candidate.get("version") != VERSION
                or not _time(candidate.get("created_at")) or _time(candidate["created_at"]) >= clock
                or candidate.get("contract_hash") != _digest({key: candidate.get(key) for key in CONTRACT_FIELDS})):
            continue
        if candidate["state"] == "promoted" and (not _time(candidate.get("valid_until")) or _time(candidate["valid_until"]) <= clock):
            continue
        kind = candidate.get("kind")
        if kind not in KINDS:
            continue
        stamp = {"candidate_id": candidate["candidate_id"], "created_at": candidate["created_at"],
                       "candidate_decision_at": clock.isoformat(), "scope": deepcopy(scope), "kind": kind,
                       "proposal": deepcopy(candidate["proposal"]), "contract_hash": candidate["contract_hash"],
                       "base_probability": probability,
                       "size_multiplier": .9 if kind == "SIZE_DOWN_WEAK_SIGNAL" and probability < .60 else 1.}
        if kind == "SIZE_DOWN_WEAK_SIGNAL":
            execution = size_execution(probability, fraction=row.get("fraction"),
                                       step=row.get("position_step"), cap=row.get("max_fraction"))
            if execution:
                stamp.update(size_execution=execution, size_multiplier=execution["effective_multiplier"])
        result.append(stamp)
    return result
