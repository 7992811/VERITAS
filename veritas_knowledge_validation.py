"""Prospective, bounded validation of audited knowledge rules.

Only a declared reduction of an existing paper position is evaluated. Direction
accuracy is not profit evidence. Net evidence replays the SAME observed path at
the prospectively declared, quantized exposure, including its fees and funding.
No order, network request, legacy backtest gate or background thread lives here.
"""
from collections import OrderedDict
from contextlib import contextmanager
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import hashlib
from itertools import islice
import json
import math
from statistics import NormalDist
import threading

import veritas_learning_exports as EXPORTS
import veritas_learning_integrity as LI
import veritas_learning_state as STORE

VERSION = "KNOWLEDGE_PROSPECTIVE_V1"
PROTOCOL = "REDUCE_WHEN_RULE_OPPOSES_BASE_V1"
PROOF_KIND = "SIMULATED_RULE_REDUCTION_ON_OBSERVED_PATH"
SNAPSHOT_NAME = "knowledge_validation"
CATALOG_JOB = "knowledge_validation_catalog"
CATALOG_VERSION = "KNOWLEDGE_CATALOG_METADATA_V2"
CATALOG_RULE_BATCH = 8
CATALOG_AUDIT_BATCH = 32
MAX_BATCH = 32
MAX_TRIALS = 8
MAX_RULES = 128
MAX_HOT_TRIALS = 32
MAX_PUBLIC_TRIALS = 32
MAX_BOARD_BYTES = 196608
MIN_TRAIN = 80
LOOKS = (100, 200, 400)
MIN_DAYS = 14
MAX_DAYS = 128
VALID_DAYS = 7
CATALOG_TTL = 3600
MAX_FAMILY = 4096
# A finite lifetime family, including unsuccessful/expired trials. The fixed
# budget is declared before the first outcome; counters are never reset by
# restarts, evictions or a change of the protocol version.
CRITERIA = dict(train=MIN_TRAIN, oos_looks=list(LOOKS), minimum_utc_days=MIN_DAYS,
                maximum_utc_days=MAX_DAYS, family_capacity=MAX_FAMILY,
                family_alpha=.05, per_test_alpha=.05/(MAX_FAMILY*len(LOOKS)*3),
                direction_hit_rate=.55, net_profit_factor=1.1, valid_days=VALID_DAYS,
                inference="FIXED_LOOK_DAILY_BLOCK_NORMAL_APPROXIMATION",
                proof_level="ABSTRACT_AUDIT_AND_PROSPECTIVE_PAPER_VALIDATION")
_Z = NormalDist().inv_cdf(1-CRITERIA["per_test_alpha"])
_LOCK = threading.RLock()
_RULES = OrderedDict()
_TRIALS = OrderedDict()
_BOARD = {}
_VERIFIED_EPOCH = None
_DIRECTION = {"LONG": "LONG", "SHORT": "SHORT", "LONG_BIAS": "LONG", "SHORT_BIAS": "SHORT"}
_MATCH_FIELDS = ("version", "rule_key", "rule_id", "source_id", "definition_hash", "audit_hash",
                 "registered_at", "action", "contract_hash")
_CONTRACT_FIELDS = ("version", "protocol", "rule_key", "rule_id", "definition_hash", "audit_hash",
                    "rule_registered_at", "registered_at", "scope", "action", "epoch", "criteria")


def _hash(value):
    return hashlib.sha256(STORE._json(value, 32768).encode()).hexdigest()


def _time(value):
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return value.astimezone(timezone.utc) if isinstance(value, datetime) and value.tzinfo else None


def _clock(value=None):
    value = datetime.now(timezone.utc) if value is None else _time(value)
    if value is None:
        raise ValueError("timezone-aware knowledge validation clock required")
    return value


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (ValueError, TypeError, OverflowError):
        return None


def _check(context):
    if context is not None:
        context.check()


@contextmanager
def _transaction(connect, context=None):
    _check(context)
    with connect() as c, c.transaction():
        timeout = max(1, min(2000, int(getattr(context, "sql_timeout_ms", 2000))))
        c.execute("SET LOCAL statement_timeout = '"+str(timeout)+"ms'")
        c.execute("SET LOCAL lock_timeout = '250ms'")
        yield c
        _check(context)


def _remember(cache, key, value, limit):
    cache[key] = deepcopy(value)
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def update_integrity_verification(status):
    """Accept only a completed same-process full receipt sweep, never storage."""
    state = LI.memory_state()
    ready = (isinstance(status, dict) and status.get("evidence_revalidation_pending") is False
             and state["ready"] and status.get("verified_process_epoch") == state["process_epoch"]
             and status.get("verified_revocation_generation") == state["revocation_generation"])
    with _LOCK:
        global _VERIFIED_EPOCH
        _VERIFIED_EPOCH = [state["process_epoch"], state["revocation_generation"]] if ready else None
    return ready


def _integrity_current():
    before = LI.memory_state()
    return (before["ready"] and _VERIFIED_EPOCH == [before["process_epoch"], before["revocation_generation"]]
            and EXPORTS.memory_current(_BOARD) and LI.memory_state() == before)


def _definition(rule):
    fields = ("rule_id", "source_id", "agent", "asset_scope", "horizons", "action", "conditions",
              "prior_weight", "hypothesis", "mechanism", "formalization_note")
    result = {k: rule.get(k) for k in fields}
    STORE._json(result, 8192)
    if (not all(isinstance(result.get(k), str) and result[k] for k in ("rule_id", "source_id", "action"))
            or result["action"] not in _DIRECTION or not isinstance(result["conditions"], list)
            or not 1 <= len(result["conditions"]) <= 12
            or not isinstance(result["asset_scope"], list) or not isinstance(result["horizons"], list)):
        raise ValueError("unsupported or incomplete immutable rule")
    return result


def _registration(rule, source, audit, clock, *, audit_revision=None):
    definition = _definition(rule)
    if (not isinstance(audit, dict) or audit.get("decision") != "USE"
            or audit.get("evidence_strength") not in ("HIGH", "MEDIUM", "LOW")
            or not isinstance(audit.get("supported_fields"), list)
            or not audit.get("claim") or not audit.get("rationale")):
        raise ValueError("original USE source audit required")
    if any(not isinstance(x, dict) or x.get("field") not in audit["supported_fields"] for x in definition["conditions"]):
        raise ValueError("rule conditions exceed audited source fields")
    STORE._json(audit, 4096)
    source = {k: source.get(k) for k in ("source_id", "title", "authors", "year", "source_type", "url", "claim", "evidence_grade")}
    STORE._json(source, 4096)
    if source["source_id"] != definition["source_id"] or not source.get("url") or not source.get("title"):
        raise ValueError("original source identity required")
    dh, ah = _hash(definition), _hash(dict(audit=audit, source=source))
    if audit_revision is not None and (type(audit_revision) is not int or audit_revision < 1):
        raise ValueError("invalid source audit revision")
    key_parts = [VERSION, dh, ah]+([audit_revision] if audit_revision is not None else [])
    result = dict(version=VERSION, rule_key=_hash(key_parts), rule_id=definition["rule_id"],
                  source_id=source["source_id"], definition_hash=dh, audit_hash=ah,
                  registered_at=clock.isoformat(), action=definition["action"],
                  definition=definition, source=source, audit=deepcopy(audit), active=True,
                  checked_at=clock.isoformat())
    if audit_revision is not None:
        result["audit_revision"] = audit_revision
    result["contract_hash"] = _hash({k: result[k] for k in _MATCH_FIELDS if k != "contract_hash"})
    return result


def _rule_valid(rule, clock):
    try:
        checked, registered = _time(rule.get("checked_at")), _time(rule.get("registered_at"))
        return (rule.get("active") is True and rule.get("version") == VERSION
                and checked is not None and registered is not None and registered <= clock
                and timedelta(0) <= clock-checked <= timedelta(seconds=CATALOG_TTL)
                and rule.get("definition_hash") == _hash(rule["definition"])
                and rule.get("audit_hash") == _hash(dict(audit=rule["audit"], source=rule["source"]))
                and rule.get("rule_key") == _hash([VERSION, rule["definition_hash"], rule["audit_hash"]]
                    +([rule["audit_revision"]] if "audit_revision" in rule else []))
                and rule.get("contract_hash") == _hash({k: rule[k] for k in _MATCH_FIELDS if k != "contract_hash"}))
    except (KeyError, ValueError, TypeError):
        return False


def capture(rule, source=None, *, audit=None, now=None):
    """Pure RAM lookup; only a durably registered, current audited rule seals.

    `source`/`audit` are optional consistency checks, never a way to register an
    unpersisted rule on the decision path. Missing audit/catalog means no trial.
    """
    clock = _clock(now)
    if not isinstance(rule, dict):
        return None
    with _LOCK:
        candidates = [r for r in _RULES.values() if r.get("rule_id") == rule.get("rule_id")]
        for registered in candidates:
            try:
                if not _rule_valid(registered, clock) or _hash(_definition(rule)) != registered["definition_hash"]:
                    continue
                if audit is not None and audit != registered["audit"]:
                    continue
                if source is not None and source.get("source_id") != registered["source_id"]:
                    continue
                return {k: deepcopy(registered[k]) for k in _MATCH_FIELDS}
            except (ValueError, TypeError):
                continue
    return None


def size_execution(action, base_direction, *, fraction, position_step, max_fraction):
    before, step, cap = map(_number, (fraction, position_step, max_fraction))
    if (base_direction not in ("LONG", "SHORT") or action not in _DIRECTION
            or any(x is None or x <= 0 for x in (before, step, cap)) or before > cap):
        return None
    requested = .9 if _DIRECTION[action] != base_direction else 1.
    quantized = math.floor(before*requested/step+1e-9)*step
    after = quantized if requested < 1 and .9*before-1e-10 <= quantized <= min(before, cap)+1e-10 else before
    return dict(fraction_before=before, fraction_after=after, position_step=step,
                max_fraction=cap, effective_multiplier=after/before)


def _scope(asset, horizon, regime, policy_hash, source_identity):
    return dict(asset=asset, horizon=horizon, regime=regime or "UNKNOWN", policy_hash=policy_hash,
                source_key=_hash(source_identity) if source_identity else "")


def _base_key(rule_key, scope):
    return _hash([rule_key, scope])


def _template(match, scope, *, epoch=0, registered_at=None):
    result = {k: match[k] for k in ("version", "rule_key", "rule_id", "definition_hash", "audit_hash", "action")}
    result.update(protocol=PROTOCOL, scope=deepcopy(scope), epoch=epoch,
                  rule_registered_at=match["registered_at"], registered_at=registered_at or match["registered_at"],
                  criteria=deepcopy(CRITERIA))
    result["contract_hash"] = _hash({k: result[k] for k in _CONTRACT_FIELDS})
    result["trial_id"] = result["contract_hash"]
    return result


def adjust(matches, *, asset, horizon, regime="UNKNOWN", policy_hash, source_identity,
           base_direction, fraction=None, position_step=None, max_fraction=None, now=None):
    """Freeze matched opportunities; a current proof can only reduce SIZE.

    All aligned opportunities are included too (candidate == baseline). Combine
    this multiplier with other controllers using min(), NEVER multiplication.
    `contribution` is always zero: this proof does not validate committee scores.
    """
    clock = _clock(now)
    out = dict(version=VERSION, status="COLLECTING", contribution=0., size_multiplier=1.,
               trials=[], applied_trial_ids=[], reasons=[], reduce_only=True)
    scope = _scope(asset, horizon, regime, policy_hash, source_identity)
    if not asset or not horizon or not policy_hash or not source_identity or base_direction not in ("LONG", "SHORT"):
        out["reasons"] = ["MISSING_PROSPECTIVE_SCOPE"]
        return out
    with _LOCK:
        seen = set()
        for match in list(islice(matches or (), MAX_RULES)):
            if not isinstance(match, dict) or match.get("rule_key") in seen:
                continue
            rule = _RULES.get(match.get("rule_key"))
            if (not rule or not _rule_valid(rule, clock)
                    or any(match.get(k) != rule.get(k) for k in _MATCH_FIELDS)):
                continue
            if asset not in rule["definition"]["asset_scope"] and not (asset == "NQ" and "NDX" in rule["definition"]["asset_scope"]):
                continue
            if horizon not in rule["definition"]["horizons"]:
                continue
            prior = _TRIALS.get(_base_key(rule["rule_key"], scope))
            if prior and (prior.get("version") != VERSION or prior.get("protocol") != PROTOCOL
                          or prior.get("scope") != scope or prior.get("criteria") != CRITERIA
                          or prior.get("contract_hash") != _hash({k: prior.get(k) for k in _CONTRACT_FIELDS})):
                continue
            template = _template(match, scope)
            phase, evaluation = "TRAIN", None
            if prior:
                if prior["state"] in ("REJECTED", "REVOKED", "EXPIRED"):
                    retry = _time(prior.get("retry_after"))
                    if not retry or clock < retry:
                        continue
                    template = _template(match, scope, epoch=prior["epoch"]+1, registered_at=retry.isoformat())
                else:
                    template = {k: deepcopy(prior[k]) for k in (*_CONTRACT_FIELDS, "contract_hash", "trial_id")}
                    phase = "MONITOR" if prior["state"] == "PROMOTED" else ("OOS" if prior.get("evaluation_id") else "TRAIN")
                    evaluation = prior.get("evaluation_id")
            execution = size_execution(rule["action"], base_direction, fraction=fraction,
                                       position_step=position_step, max_fraction=max_fraction)
            # Compact stamp: criteria are identified by contract_hash and VERSION,
            # not duplicated eight times in every entry payload.
            stamp = {k: deepcopy(v) for k, v in template.items() if k != "criteria"}
            stamp.update(decision_at=clock.isoformat(), phase=phase, evaluation_id=evaluation,
                         base_direction=base_direction, size_execution=execution)
            out["trials"].append(stamp)
            seen.add(rule["rule_key"])
            if (prior and prior["state"] == "PROMOTED" and prior.get("evidence_valid") is True
                    and _time(prior.get("valid_until")) and clock < _time(prior["valid_until"])
                    and prior.get("direction_pass") is True and prior.get("net_pass") is True
                    and _integrity_current() and execution):
                out["size_multiplier"] = min(out["size_multiplier"], execution["effective_multiplier"])
                if execution["effective_multiplier"] < 1:
                    out["applied_trial_ids"].append(prior["trial_id"])
            if len(out["trials"]) >= MAX_TRIALS:
                break
    if out["applied_trial_ids"]:
        out["status"] = "APPLIED_REDUCTION"
    else:
        out["reasons"] = ["AWAIT_CURRENT_DIRECTION_AND_NET_PAPER_PROOF"]
    return out


def _stats():
    return dict(n=0, wins=0, sum=0., gains=0., losses=0., days={}, evidence_hash=None,
                base_sum=0., delta_sum=0., equity=0., peak=0., drawdown=0.,
                base_equity=0., base_peak=0., base_drawdown=0.)


def _add(stats, row, value, base=None):
    day = row["outcome_at"][:10]
    if day not in stats["days"] and len(stats["days"]) >= MAX_DAYS:
        return False
    block = stats["days"].setdefault(day, [0, 0., 0., 0.])
    block[0] += 1; block[1] += value; block[2] += int(value > 0)
    delta = 0. if base is None else value-base
    block[3] += delta
    stats["n"] += 1; stats["wins"] += int(value > 0); stats["sum"] += value
    stats["gains"] += max(0., value); stats["losses"] += max(0., -value)
    stats["base_sum"] += base if base is not None else 0.; stats["delta_sum"] += delta
    stats["evidence_hash"] = _hash([stats["evidence_hash"], row["evidence_hash"]])
    for prefix, amount in (("", value), ("base_", base if base is not None else 0.)):
        stats[prefix+"equity"] += amount
        stats[prefix+"peak"] = max(stats[prefix+"peak"], stats[prefix+"equity"])
        stats[prefix+"drawdown"] = max(stats[prefix+"drawdown"], stats[prefix+"peak"]-stats[prefix+"equity"])
    return True


def _lower(values):
    n = len(values)
    if n < 2:
        return None
    mean = sum(values)/n
    variance = sum((v-mean)**2 for v in values)/(n-1)
    return mean-_Z*math.sqrt(variance/n)


def _summary(stats):
    n = stats["n"]
    return dict(n=n, days=len(stats["days"]), mean=stats["sum"]/n if n else None,
                hit_rate=stats["wins"]/n if n else None,
                profit_factor=(stats["gains"]/stats["losses"] if stats["losses"] else None),
                no_observed_losses=bool(n and stats["losses"] == 0),
                mean_lower=_lower([d[1]/d[0] for d in stats["days"].values()]),
                hit_lower=_lower([d[2]/d[0] for d in stats["days"].values()]),
                delta_lower=_lower([d[3]/d[0] for d in stats["days"].values()]),
                mean_delta=stats["delta_sum"]/n if n else None,
                drawdown_r=stats["drawdown"], baseline_drawdown_r=stats["base_drawdown"],
                evidence_hash=stats["evidence_hash"])


def _new_trial(stamp):
    contract = {k: deepcopy(stamp.get(k)) for k in _CONTRACT_FIELDS}
    contract["criteria"] = deepcopy(CRITERIA)
    if stamp.get("contract_hash") != _hash(contract) or stamp.get("trial_id") != stamp.get("contract_hash"):
        raise ValueError("knowledge trial contract mismatch")
    return dict(contract, trial_id=stamp["trial_id"], contract_hash=stamp["contract_hash"],
                state="TRAINING", evidence_valid=True, reasons=["AWAIT_80_PROSPECTIVE_TRAIN"],
                training=_stats(), direction=_stats(), net=_stats(),
                monitor_direction=_stats(), monitor_net=_stats(),
                evaluation_id=None, oos_registered_at=None, direction_pass=False, net_pass=False,
                direction_looks=[], net_looks=[], valid_until=None, retry_after=None, monitor_registered_at=None)


def _terminate(candidate, state, reason, clock):
    candidate.update(state=state, evidence_valid=False, valid_until=None,
                     reasons=[reason], retry_after=(clock+timedelta(days=VALID_DAYS)).isoformat())


def _normalized(raw, stamp, candidate, clock):
    try:
        STORE._json(stamp, 2048)
    except (ValueError, TypeError):
        return None, "OVERSIZED_TRIAL_STAMP"
    if any(stamp.get(k) != candidate.get(k) for k in _CONTRACT_FIELDS if k != "criteria") or stamp.get("contract_hash") != candidate["contract_hash"]:
        return None, "IMMUTABLE_CONTRACT_MISMATCH"
    known, decision, outcome, observed = map(_time, (raw.get("known_at"), raw.get("decision_at"), raw.get("outcome_at"), raw.get("observed_at")))
    stamped, registered = _time(stamp.get("decision_at")), _time(stamp.get("registered_at"))
    if (not all((known, decision, outcome, observed, stamped, registered))
            or not (registered <= stamped <= decision < outcome <= observed <= clock and known <= stamped)):
        return None, "NONPROSPECTIVE_OR_INCOMPLETE_TIMES"
    if (raw.get("evidence_valid") is not True or raw.get("source_verified") is not True
            or raw.get("independence_verified") is not True or not raw.get("evidence_hash")
            or not raw.get("evidence_version") or not (raw.get("idea_id") or raw.get("event_id"))):
        return None, "UNVERIFIED_OUTCOME"
    if _scope(raw.get("asset"), raw.get("horizon"), raw.get("regime"), raw.get("policy_hash"), raw.get("source_identity")) != candidate["scope"]:
        return None, "SOURCE_OR_POLICY_SCOPE_MISMATCH"
    if raw.get("direction") != stamp.get("base_direction"):
        return None, "BASE_DIRECTION_MISMATCH"
    trade = bool(raw.get("proof_kind"))
    row = dict(outcome_at=outcome.isoformat(), decision_at=decision.isoformat(), evidence_hash=raw["evidence_hash"], channel="net" if trade else "direction")
    if trade:
        flows = raw.get("knowledge_cashflows")
        if not isinstance(flows, dict) or flows.get("proof_kind") != PROOF_KIND:
            return None, "MISSING_PAIRED_KNOWLEDGE_CASHFLOWS"
        if (raw.get("costs_verified") is not True or raw.get("risk_verified") is not True
                or flows.get("cashflows_verified") is not True or flows.get("source_evidence_hash") != raw["evidence_hash"]):
            return None, "UNVERIFIED_NET_COST_OR_RISK"
        execution = stamp.get("size_execution")
        if not isinstance(execution, dict):
            return None, "MISSING_FROZEN_SIZE_EXECUTION"
        expected = size_execution(candidate["action"], stamp["base_direction"], fraction=execution.get("fraction_before"),
                                  position_step=execution.get("position_step"), max_fraction=execution.get("max_fraction"))
        if execution != expected:
            return None, "INVALID_FROZEN_SIZE_EXECUTION"
        if any(_number(flows.get(k)) != execution[k] for k in ("fraction_before", "position_step", "max_fraction")):
            return None, "OBSERVED_EXPOSURE_DOES_NOT_MATCH_DECLARED_TRIAL"
        base, risk, cap = map(_number, (flows.get("baseline_net_r"), flows.get("baseline_risk_r"), flows.get("risk_cap_r")))
        if base is None or risk != 1. or cap is None or cap < risk or abs(base) > 100:
            return None, "UNVERIFIED_COMMON_NET_RISK_UNITS"
        # Root's verified trade producer reconciles original gross-fees-funding
        # and exposure. No AUTO candidate's cashflows are relabelled here.
        if _number(raw.get("baseline_net_r")) != base:
            return None, "BASELINE_CASHFLOW_MISMATCH"
        row.update(value=base*execution["effective_multiplier"], base=base)
    else:
        forward = _number(raw.get("forward_return"))
        if forward is None or abs(forward) > 100:
            return None, "MISSING_DIRECTION_OUTCOME"
        row.update(value=forward*(1 if _DIRECTION[candidate["action"]] == "LONG" else -1), base=None)
    return row, None


def evaluate(candidate, raw, stamp, *, now=None):
    """Pure candidate transition; durable caller owns dedup and nonoverlap."""
    clock = _clock(now)
    result = deepcopy(candidate)
    if result["contract_hash"] != _hash({k: result[k] for k in _CONTRACT_FIELDS}):
        _terminate(result, "REVOKED", "IMMUTABLE_CONTRACT_MISMATCH", clock)
        return result, "IMMUTABLE_CONTRACT_MISMATCH"
    if result["state"] in ("REJECTED", "REVOKED", "EXPIRED"):
        return result, "TERMINAL_TRIAL"
    row, reason = _normalized(raw, stamp, result, clock)
    if reason:
        return result, reason
    if result["state"] == "PROMOTED" and _time(result["valid_until"]) <= clock:
        _terminate(result, "EXPIRED", "PROOF_EXPIRED", clock)
        return result, "PROOF_EXPIRED"
    if result["state"] == "TRAINING":
        if stamp.get("phase") != "TRAIN" or row["channel"] != "direction":
            return result, "AWAIT_DIRECTION_TRAINING"
        if not _add(result["training"], row, row["value"]):
            _terminate(result, "REJECTED", "TRAINING_DAY_BUDGET_EXHAUSTED", clock)
            return result, "TRAINING_DAY_BUDGET_EXHAUSTED"
        if result["training"]["n"] == MIN_TRAIN:
            training = _summary(result["training"])
            if training["hit_rate"] < .55 or training["mean"] <= 0:
                _terminate(result, "REJECTED", "DIRECTION_TRAINING_NOT_SUPPORTED", clock)
            else:
                result.update(state="OOS", oos_registered_at=clock.isoformat(),
                              evaluation_id=_hash([result["contract_hash"], result["training"]["evidence_hash"], clock.isoformat()]),
                              reasons=["AWAIT_100_FUTURE_DIRECTION_AND_PAIRED_NET"])
        return result, None
    if (stamp.get("evaluation_id") != result["evaluation_id"]
            or stamp.get("phase") not in ("OOS", "MONITOR")
            or _time(row["decision_at"]) <= _time(result["oos_registered_at"])):
        return result, "OOS_NOT_PREREGISTERED_BEFORE_DECISION"
    prefix = "monitor_" if result["state"] == "PROMOTED" else ""
    if prefix and (stamp.get("phase") != "MONITOR" or not _time(result.get("monitor_registered_at"))
                   or _time(row["decision_at"]) <= _time(result["monitor_registered_at"])):
        return result, "MONITOR_NOT_PREREGISTERED_BEFORE_DECISION"
    stats = result[prefix+row["channel"]]
    if stats["n"] >= LOOKS[-1]:
        return result, "FIXED_LOOK_SAMPLE_COMPLETE"
    if not _add(stats, row, row["value"], row["base"]):
        _terminate(result, "REJECTED", "OOS_DAY_BUDGET_EXHAUSTED", clock)
        return result, "OOS_DAY_BUDGET_EXHAUSTED"
    if prefix:
        if result["monitor_direction"]["n"] >= 32 and result["monitor_net"]["n"] >= 32:
            d, n = _summary(result["monitor_direction"]), _summary(result["monitor_net"])
            if d["hit_rate"] < .5 or n["mean_delta"] < 0 or n["mean"] <= 0:
                _terminate(result, "REVOKED", "RECENT_PROSPECTIVE_PAPER_DECAY", clock)
            elif d["days"] >= 7 and n["days"] >= 7:
                result.update(valid_until=(clock+timedelta(days=VALID_DAYS)).isoformat(),
                              monitor_direction=_stats(), monitor_net=_stats(), monitor_registered_at=clock.isoformat())
        return result, None
    if stats["n"] in LOOKS:
        channel = row["channel"]
        result[channel+"_looks"].append(stats["n"])
        summary = _summary(stats)
        enough = summary["days"] >= MIN_DAYS
        if channel == "direction":
            result["direction_pass"] = bool(enough and summary["hit_rate"] >= .55
                                               and summary["mean"] > 0 and summary["hit_lower"] > .5)
        else:
            pf = summary["profit_factor"]
            result["net_pass"] = bool(enough and summary["delta_lower"] > 0 and summary["mean_lower"] > 0
                    and (summary["no_observed_losses"] or pf >= 1.1)
                    and summary["drawdown_r"] <= summary["baseline_drawdown_r"]+1e-10)
        if result["direction_pass"] and result["net_pass"]:
            result.update(state="PROMOTED", valid_until=(clock+timedelta(days=VALID_DAYS)).isoformat(),
                          reasons=["PROSPECTIVE_REDUCE_ONLY_PAPER_EVIDENCE"], monitor_registered_at=clock.isoformat())
        elif stats["n"] == LOOKS[-1] and not result[channel+"_pass"]:
            _terminate(result, "REJECTED", "FIXED_LOOK_"+channel.upper()+"_NOT_SUPPORTED", clock)
        else:
            result["reasons"] = ["AWAIT_PAIRED_NET_EVIDENCE" if result["direction_pass"] else "AWAIT_PROSPECTIVE_DIRECTION_PROOF"]
    return result, None


def ensure_schema(pg_connect, *, context=None):
    STORE.ensure_schema(pg_connect)
    with _transaction(pg_connect, context) as c:
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_rules (
            rule_key text PRIMARY KEY,rule_id text NOT NULL,source_id text NOT NULL,version text NOT NULL,
            payload jsonb NOT NULL,active boolean NOT NULL,checked_at timestamptz NOT NULL,
            CHECK(octet_length(payload::text)<=24576))""")
        c.execute("CREATE INDEX IF NOT EXISTS knowledge_validation_rule_id ON knowledge_validation_rules(rule_id)")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_source_audits (
            source_id text PRIMARY KEY,candidate_id text NOT NULL,audit jsonb,audit_hash text NOT NULL,
            revision bigint NOT NULL DEFAULT 1,
            processed_at timestamptz,checked_at timestamptz NOT NULL,
            CHECK(audit IS NULL OR octet_length(audit::text)<=8192))""")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_trials (
            trial_id text PRIMARY KEY,rule_key text NOT NULL,base_key text NOT NULL,epoch integer NOT NULL,
            payload jsonb NOT NULL,updated_at timestamptz NOT NULL DEFAULT now(),
            CHECK(octet_length(payload::text)<=65536),UNIQUE(base_key,epoch))""")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_seen (
            idea_key text PRIMARY KEY,trial_id text NOT NULL,original_key text NOT NULL,evidence_hash text NOT NULL,
            valid boolean NOT NULL,observed_at timestamptz NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_streams (
            stream_key text PRIMARY KEY,last_outcome_at timestamptz NOT NULL)""")
        c.execute("""CREATE TABLE IF NOT EXISTS knowledge_validation_family (
            singleton boolean PRIMARY KEY DEFAULT TRUE CHECK(singleton),registered bigint NOT NULL DEFAULT 0)""")
        c.execute("INSERT INTO knowledge_validation_family(singleton) VALUES(TRUE) ON CONFLICT DO NOTHING")
    return {"status": "OK"}


def _catalog_cursor(value):
    if not isinstance(value, dict) or value.get("version") != CATALOG_VERSION:
        return dict(version=CATALOG_VERSION, phase="audits", candidate_after="", rule_after="", audits_complete=False)
    result = deepcopy(value)
    if result.get("phase") not in ("audits", "rules"):
        raise ValueError("invalid knowledge catalog phase")
    STORE._json(result, 4096)
    return result


def _index_audits(c, cursor, clock, context):
    # Metadata-only PK page first. Full candidate metadata is decoded only for
    # these <=32 IDs, never scanned/hash-joined separately for every rule.
    rows = c.execute("""WITH page AS MATERIALIZED (
        SELECT candidate_id,doi,processed_at FROM knowledge_candidates
        WHERE candidate_id>%s ORDER BY candidate_id LIMIT %s)
        SELECT p.candidate_id,p.doi,p.processed_at,
          CASE WHEN octet_length(a.llm_audit::text)<=4096 THEN a.llm_audit ELSE NULL END audit
        FROM page p JOIN knowledge_candidates k USING(candidate_id)
        CROSS JOIN LATERAL jsonb_to_record(CASE WHEN jsonb_typeof(k.metadata)='object'
          THEN k.metadata ELSE '{}'::jsonb END) AS a(llm_audit jsonb)
        ORDER BY p.candidate_id""", (cursor["candidate_after"], CATALOG_AUDIT_BATCH)).fetchall()
    selected = {}
    for row in rows:
        _check(context)
        sid = "AUTO_"+hashlib.sha256((row["doi"] or row["candidate_id"]).encode()).hexdigest()[:20].upper()
        stamp = _time(row["processed_at"])
        item = dict(source_id=sid, candidate_id=row["candidate_id"], audit=row["audit"],
                    audit_hash=_hash(row["audit"]), processed_at=stamp.isoformat() if stamp else None,
                    checked_at=clock.isoformat())
        old = selected.get(sid)
        # Match the prior processed_at DESC NULLS LAST, candidate_id ASC rule.
        current_key = item["processed_at"] or ""
        old_key = (old.get("processed_at") or "") if old else ""
        if not old or current_key > old_key or (current_key == old_key and item["candidate_id"] < old["candidate_id"]):
            selected[sid] = item
    changed = []
    if selected:
        old = c.execute("SELECT source_id,audit_hash FROM knowledge_validation_source_audits WHERE source_id=ANY(%s)", (list(selected),)).fetchall()
        hashes = {x["source_id"]: x["audit_hash"] for x in old}
        updates = c.execute("""INSERT INTO knowledge_validation_source_audits
            (source_id,candidate_id,audit,audit_hash,processed_at,checked_at)
            SELECT source_id,candidate_id,audit,audit_hash,processed_at,checked_at
            FROM jsonb_to_recordset(%s::jsonb) AS x(source_id text,candidate_id text,audit jsonb,
                audit_hash text,processed_at timestamptz,checked_at timestamptz)
            ON CONFLICT(source_id) DO UPDATE SET candidate_id=EXCLUDED.candidate_id,audit=EXCLUDED.audit,
                audit_hash=EXCLUDED.audit_hash,processed_at=EXCLUDED.processed_at,checked_at=EXCLUDED.checked_at
                ,revision=knowledge_validation_source_audits.revision+
                    CASE WHEN knowledge_validation_source_audits.audit_hash IS DISTINCT FROM EXCLUDED.audit_hash THEN 1 ELSE 0 END
            WHERE knowledge_validation_source_audits.candidate_id=EXCLUDED.candidate_id
               OR COALESCE(EXCLUDED.processed_at,'-infinity'::timestamptz)>COALESCE(knowledge_validation_source_audits.processed_at,'-infinity'::timestamptz)
               OR (EXCLUDED.processed_at IS NOT DISTINCT FROM knowledge_validation_source_audits.processed_at
                   AND EXCLUDED.candidate_id<knowledge_validation_source_audits.candidate_id)
            RETURNING source_id,audit_hash""", (STORE._json(list(selected.values()), 196608),)).fetchall()
        changed = [x["source_id"] for x in updates if hashes.get(x["source_id"]) != x["audit_hash"]]
        if changed:
            c.execute("UPDATE knowledge_validation_rules SET active=FALSE,checked_at=%s WHERE source_id=ANY(%s) AND active=TRUE", (clock, changed))
    more = len(rows) == CATALOG_AUDIT_BATCH
    cursor["candidate_after"] = rows[-1]["candidate_id"] if more else ""
    if not more:
        cursor["audits_complete"] = True
    cursor["phase"] = "rules" if cursor["audits_complete"] else "audits"
    return dict(status="OK" if cursor["audits_complete"] else "PROGRESS", phase="audit_index",
                processed=len(rows), indexed=len(selected), invalidated_sources=len(changed),
                audits_complete=cursor["audits_complete"]), [], [], changed


def _catalog_rules(c, cursor, clock, context):
    rows = c.execute("""WITH page AS MATERIALIZED (
        SELECT rule_id,source_id,agent,asset_scope,horizons,action,status,
          CASE WHEN octet_length(conditions::text)<=4096 THEN conditions ELSE NULL END conditions,
          prior_weight,CASE WHEN octet_length(hypothesis)<=2048 THEN hypothesis ELSE NULL END hypothesis,
          CASE WHEN octet_length(mechanism)<=2048 THEN mechanism ELSE NULL END mechanism,
          CASE WHEN octet_length(formalization_note)<=2048 THEN formalization_note ELSE NULL END formalization_note
        FROM knowledge_rules WHERE rule_id>%s AND (left(source_id,5)='AUTO_'
          OR EXISTS (SELECT 1 FROM knowledge_validation_rules known
            WHERE known.rule_id=knowledge_rules.rule_id AND known.active))
        ORDER BY rule_id LIMIT %s)
        SELECT p.*,jsonb_build_object('source_id',s.source_id,'title',left(s.title,512),
          'authors',left(s.authors,512),'year',s.year,'source_type',s.source_type,'url',left(s.url,1024),
          'claim',left(s.claim,1024),'evidence_grade',s.evidence_grade) AS source,
          a.audit,a.checked_at audit_checked_at,a.revision audit_revision
        FROM page p LEFT JOIN knowledge_sources s ON s.source_id=p.source_id
        LEFT JOIN knowledge_validation_source_audits a ON a.source_id=p.source_id
        ORDER BY p.rule_id""", (cursor["rule_after"], CATALOG_RULE_BATCH)).fetchall()
    active, rejected = [], []
    for raw in rows:
        _check(context)
        raw = dict(raw)
        try:
            checked = _time(raw.get("audit_checked_at"))
            if (raw.get("status") not in ("shadow", "validated_candidate") or not checked
                    or not timedelta(0) <= clock-checked <= timedelta(seconds=CATALOG_TTL)):
                raise ValueError("inactive or stale source audit")
            entry = _registration(raw, raw.get("source") or {}, raw.get("audit"), clock, audit_revision=raw.get("audit_revision"))
            active.append(entry)
        except (ValueError, TypeError, KeyError):
            rejected.append(raw["rule_id"])
    if active:
        prior = c.execute("SELECT rule_key,payload FROM knowledge_validation_rules WHERE rule_key=ANY(%s)", ([e["rule_key"] for e in active],)).fetchall()
        originals = {r["rule_key"]: r["payload"] for r in prior}
        active = [dict(originals.get(e["rule_key"], e), active=True, checked_at=clock.isoformat()) for e in active]
    ids = [r["rule_id"] for r in rows]
    if ids:
        c.execute("UPDATE knowledge_validation_rules SET active=FALSE,checked_at=%s WHERE rule_id=ANY(%s) AND active=TRUE", (clock, ids))
    if active:
        batch = [dict(rule_key=e["rule_key"], rule_id=e["rule_id"], source_id=e["source_id"], version=VERSION,
                      payload=e, checked_at=clock.isoformat()) for e in active]
        c.execute("""INSERT INTO knowledge_validation_rules(rule_key,rule_id,source_id,version,payload,active,checked_at)
            SELECT rule_key,rule_id,source_id,version,payload,TRUE,checked_at
            FROM jsonb_to_recordset(%s::jsonb) AS x(rule_key text,rule_id text,source_id text,version text,payload jsonb,checked_at timestamptz)
            ON CONFLICT(rule_key) DO UPDATE SET active=TRUE,checked_at=EXCLUDED.checked_at,payload=EXCLUDED.payload""",
            (STORE._json(batch, 196608),))
    cursor["rule_after"] = rows[-1]["rule_id"] if len(rows) == CATALOG_RULE_BATCH else ""
    cursor["phase"] = "audits"
    return dict(status="OK", phase="rules", processed=len(rows), audited=len(active), excluded=len(rejected)), active, ids, []


def refresh_catalog(pg_connect, *, context=None, now=None, cursor=None):
    """One <=32-audit or <=8-rule microstage, with four bulk SQL statements.

    Supplying a cursor delegates fencing/checkpointing to the calling durable
    job. Return `result['cursor']` to that caller only after success. Without a
    cursor this function owns one fenced job for standalone compatibility.
    """
    clock = _clock(now)
    lease = None
    if cursor is None:
        lease = STORE.claim_job(pg_connect, CATALOG_JOB, CATALOG_VERSION, lease_seconds=60)
        if not lease:
            raise RuntimeError("KNOWLEDGE_CATALOG_BUSY")
        cursor = lease.get("cursor")
    progress = _catalog_cursor(cursor)
    try:
        with _transaction(pg_connect, context) as c:
            operation = _catalog_rules if progress["phase"] == "rules" and progress["audits_complete"] else _index_audits
            result, active, ids, sources = operation(c, progress, clock, context)
        result.update(cursor=progress, updated_at=clock.isoformat())
        with _LOCK:
            for key in [k for k, v in _RULES.items() if v["rule_id"] in ids or v["source_id"] in sources]:
                del _RULES[key]
            for entry in active:
                _remember(_RULES, entry["rule_key"], entry, MAX_RULES)
        if lease and not STORE.checkpoint_job(pg_connect, lease, status="OK", cursor=progress, result=result):
            raise RuntimeError("knowledge catalog checkpoint rejected")
        return result
    except Exception as exc:
        if lease:
            try:
                STORE.checkpoint_job(pg_connect, lease, status="ERROR", cursor=lease.get("cursor"),
                                     result={"status": "ERROR", "error": type(exc).__name__}, retry_after_seconds=30)
            except Exception:
                pass
        raise


def _public_trial(candidate):
    return {**{k: deepcopy(candidate.get(k)) for k in ("trial_id", "rule_key", "rule_id", "scope", "action", "epoch", "state",
             "reasons", "registered_at", "oos_registered_at", "valid_until", "evidence_valid")},
            "training": _summary(candidate["training"]), "direction": _summary(candidate["direction"]),
            "net": _summary(candidate["net"]), "contribution": 0., "reduce_only": True}


def _hot_trial(candidate):
    # Advice needs the frozen contract and eligibility, not five daily-stat
    # maps. Keeping those in SQL avoids a second retained decoded archive.
    keys = (*_CONTRACT_FIELDS, "contract_hash", "trial_id", "state", "evidence_valid",
            "evaluation_id", "oos_registered_at", "monitor_registered_at", "valid_until", "retry_after", "direction_pass", "net_pass")
    return {k: deepcopy(candidate.get(k)) for k in keys}


def _materialize_board(board, candidates, clock, family, integrity_token):
    board.update(updated_at=clock.isoformat(), registered_trials=family,
                 status="COLLECTING", candidates=[_public_trial(x) for x in candidates[:MAX_PUBLIC_TRIALS]],
                 hot_trial_count=len(candidates), visible_trial_count=min(len(candidates), MAX_PUBLIC_TRIALS),
                 proof_level=CRITERIA["proof_level"], criteria=CRITERIA,
                 contribution=0., reduce_only=True, real_orders_enabled=False, **integrity_token)
    board["profiles"] = [{k: deepcopy(x.get(k)) for k in ("trial_id", "rule_key", "rule_id", "scope", "state", "valid_until", "evidence_valid")}
                         for x in candidates if x["state"] == "PROMOTED" and x["evidence_valid"]][:MAX_PUBLIC_TRIALS]
    if board["profiles"]:
        board["status"] = "PROVEN_PAPER_REDUCTION"
    STORE._json(board, MAX_BOARD_BYTES)
    return board


def _put_trial(c, candidate, clock):
    c.execute("""INSERT INTO knowledge_validation_trials(trial_id,rule_key,base_key,epoch,payload,updated_at)
        VALUES(%s,%s,%s,%s,%s::jsonb,%s) ON CONFLICT(trial_id) DO UPDATE SET payload=EXCLUDED.payload,updated_at=EXCLUDED.updated_at""",
        (candidate["trial_id"], candidate["rule_key"], _base_key(candidate["rule_key"], candidate["scope"]),
         candidate["epoch"], STORE._json(candidate, 60000), clock))


def run_batch(pg_connect, observations=(), *, now=None, context=None):
    """Consume <=32 sealed rows atomically; caller acknowledges only on success.

    Receipt deduplication, immutable candidate transitions and the compact
    checkpoint share a transaction. Retrying after commit cannot inflate counts.
    """
    clock = _clock(now)
    integrity_token = EXPORTS.memory_contract()
    rows = list(islice(observations, MAX_BATCH+1))
    if len(rows) > MAX_BATCH:
        raise ValueError("knowledge batch exceeds 32 observations")
    rows.sort(key=lambda r: str(r.get("decision_at") or ""))
    with _transaction(pg_connect, context) as c:
        prior = STORE.load_snapshot_in_transaction(c, SNAPSHOT_NAME, VERSION, for_update=True)
        board = deepcopy(prior["payload"]) if prior else dict(version=VERSION, status="COLLECTING", counts={}, reasons={})
        family = c.execute("SELECT registered FROM knowledge_validation_family WHERE singleton=TRUE FOR UPDATE").fetchone()["registered"]
        for raw in rows:
            _check(context)
            stamps = raw.get("knowledge_trials") or []
            if not isinstance(stamps, list) or len(stamps) > MAX_TRIALS:
                raise ValueError("knowledge trial count exceeds eight")
            for stamp in stamps:
                if not isinstance(stamp, dict) or not raw.get("asset") or not (raw.get("idea_id") or raw.get("event_id")):
                    continue
                _check(context)
                STORE._json(stamp, 2048)
                cid = stamp.get("trial_id")
                channel = "net" if raw.get("proof_kind") else "direction"
                key = _hash([stamp.get("rule_key"), raw["asset"], raw.get("idea_id") or raw["event_id"], channel])
                original = str(raw.get("episode_key") or raw.get("trade_id") or key)
                evidence = str(raw.get("evidence_hash") or "MISSING")
                seen = c.execute("SELECT * FROM knowledge_validation_seen WHERE idea_key=%s", (key,)).fetchone()
                if seen:
                    revised = seen["original_key"] == original and seen["evidence_hash"] != evidence
                    invalidated = any(raw.get(k) is not True for k in ("evidence_valid", "source_verified", "independence_verified"))
                    if channel == "net":
                        invalidated |= any(raw.get(k) is not True for k in ("costs_verified", "risk_verified"))
                    if seen["valid"] and (revised or invalidated):
                        old = c.execute("SELECT payload FROM knowledge_validation_trials WHERE trial_id=%s FOR UPDATE", (seen["trial_id"],)).fetchone()
                        if old:
                            candidate = dict(old["payload"])
                            _terminate(candidate, "REVOKED", "SOURCE_EVIDENCE_INVALIDATED", clock)
                            _put_trial(c, candidate, clock)
                        c.execute("UPDATE knowledge_validation_seen SET valid=FALSE WHERE idea_key=%s", (key,))
                        board["counts"]["revoked"] = board["counts"].get("revoked", 0)+1
                    continue
                reg = c.execute("SELECT payload,active,checked_at FROM knowledge_validation_rules WHERE rule_key=%s", (stamp.get("rule_key"),)).fetchone()
                reason = None
                if not reg or not _rule_valid(dict(reg["payload"], active=reg["active"], checked_at=reg["checked_at"].isoformat()), clock):
                    reason = "AUDITED_RULE_UNAVAILABLE_OR_STALE"
                stored = c.execute("SELECT payload FROM knowledge_validation_trials WHERE trial_id=%s FOR UPDATE", (cid,)).fetchone()
                if stored:
                    candidate = deepcopy(stored["payload"])
                elif reason is None:
                    candidate = _new_trial(stamp)
                    match = reg["payload"]
                    if any(candidate.get(k) != match.get(k) for k in ("rule_id", "definition_hash", "audit_hash", "action")) or candidate["rule_registered_at"] != match["registered_at"]:
                        reason = "REGISTERED_RULE_CONTRACT_MISMATCH"
                    elif candidate["epoch"] != 0:
                        previous = c.execute("SELECT payload FROM knowledge_validation_trials WHERE base_key=%s AND epoch=%s", (_base_key(candidate["rule_key"], candidate["scope"]), candidate["epoch"]-1)).fetchone()
                        if (not previous or previous["payload"]["state"] not in ("REJECTED", "REVOKED", "EXPIRED")
                                or previous["payload"].get("retry_after") != candidate["registered_at"]):
                            reason = "UNREGISTERED_TRIAL_RETRY"
                    if reason is None and family >= MAX_FAMILY:
                        reason = "LIFETIME_FAMILY_BUDGET_EXHAUSTED"
                    if reason is None:
                        family += 1
                else:
                    candidate = None
                if reason is None:
                    stream_key = _hash([candidate["rule_key"], raw["asset"], channel])
                    stream = c.execute("SELECT last_outcome_at FROM knowledge_validation_streams WHERE stream_key=%s", (stream_key,)).fetchone()
                    decision = _time(raw.get("decision_at"))
                    if stream and decision and decision < stream["last_outcome_at"]:
                        reason = "OVERLAPPING_MARKET_IDEA"
                    else:
                        candidate, reason = evaluate(candidate, raw, stamp, now=clock)
                    _put_trial(c, candidate, clock)
                    if reason is None:
                        c.execute("""INSERT INTO knowledge_validation_streams(stream_key,last_outcome_at) VALUES(%s,%s)
                            ON CONFLICT(stream_key) DO UPDATE SET last_outcome_at=EXCLUDED.last_outcome_at""", (stream_key, _time(raw["outcome_at"])))
                c.execute("INSERT INTO knowledge_validation_seen(idea_key,trial_id,original_key,evidence_hash,valid,observed_at) VALUES(%s,%s,%s,%s,%s,%s)",
                          (key, cid or "INVALID", original, evidence, reason is None, clock))
                label = reason or channel
                target = board["reasons"] if reason else board["counts"]
                target[label] = target.get(label, 0)+1
        c.execute("UPDATE knowledge_validation_family SET registered=%s WHERE singleton=TRUE", (family,))
        # Materialize only a bounded hot board; full immutable trials stay in SQL.
        hot = c.execute("SELECT payload FROM knowledge_validation_trials ORDER BY updated_at DESC,trial_id LIMIT %s", (MAX_HOT_TRIALS,)).fetchall()
        candidates = [dict(row["payload"]) for row in hot]
        for candidate in candidates:
            if candidate["state"] == "PROMOTED" and _time(candidate["valid_until"]) <= clock:
                _terminate(candidate, "EXPIRED", "PROOF_EXPIRED", clock)
                _put_trial(c, candidate, clock)
        board = _materialize_board(board, candidates, clock, family, integrity_token)
        if integrity_token != EXPORTS.memory_contract():
            raise RuntimeError("knowledge integrity changed during batch")
        if not STORE.publish_snapshot_in_transaction(c, SNAPSHOT_NAME, VERSION, board, observed_at=clock,
                                                     watermark=str(sum(board["counts"].values()))):
            raise RuntimeError("knowledge proof checkpoint rejected")
    with _LOCK:
        global _BOARD
        _BOARD = deepcopy(board)
        for candidate in candidates:
            key = _base_key(candidate["rule_key"], candidate["scope"])
            if key not in _TRIALS or _TRIALS[key]["epoch"] <= candidate["epoch"]:
                _remember(_TRIALS, key, _hot_trial(candidate), MAX_HOT_TRIALS)
    return snapshot(now=clock)


def restore(pg_connect, *, context=None, now=None):
    clock = _clock(now)
    with _transaction(pg_connect, context) as c:
        rules = c.execute("SELECT payload,active,checked_at FROM knowledge_validation_rules WHERE version=%s AND active=TRUE AND checked_at>=%s ORDER BY checked_at DESC,rule_key LIMIT %s",
                          (VERSION, clock-timedelta(seconds=CATALOG_TTL), MAX_RULES)).fetchall()
        trials = c.execute("SELECT payload FROM knowledge_validation_trials ORDER BY updated_at DESC,trial_id LIMIT %s", (MAX_HOT_TRIALS,)).fetchall()
        board = STORE.load_snapshot_in_transaction(c, SNAPSHOT_NAME, VERSION)
    with _LOCK:
        global _BOARD, _VERIFIED_EPOCH
        _RULES.clear(); _TRIALS.clear()
        for row in rules:
            entry = dict(row["payload"], active=row["active"], checked_at=row["checked_at"].isoformat())
            if _rule_valid(entry, clock):
                _remember(_RULES, entry["rule_key"], entry, MAX_RULES)
        for row in reversed(trials):
            entry = row["payload"]
            _remember(_TRIALS, _base_key(entry["rule_key"], entry["scope"]), _hot_trial(entry), MAX_HOT_TRIALS)
        _BOARD = deepcopy(board["payload"]) if board else {}
        _VERIFIED_EPOCH = None
        # Previous-process integrity seals cannot authorize live advice.
        _BOARD["restored"] = True
    return snapshot(now=clock)


def snapshot(*, now=None):
    clock = _clock(now)
    with _LOCK:
        result = deepcopy(_BOARD) or dict(version=VERSION, status="COLLECTING", counts={}, candidates=[], profiles=[])
        result["catalog_audited_rules"] = sum(_rule_valid(r, clock) for r in _RULES.values())
        result["profiles"] = [p for p in result.get("profiles", []) if _time(p.get("valid_until"))
                              and clock < _time(p["valid_until"])
                              and p.get("rule_key") in _RULES and _rule_valid(_RULES[p["rule_key"]], clock)]
        result["advice_current"] = _integrity_current()
        if not result["advice_current"]:
            result["profiles"] = []
            result["status"] = "AWAIT_RECEIPT_REVALIDATION" if result.get("status") == "PROVEN_PAPER_REDUCTION" else result.get("status", "COLLECTING")
        result.update(contribution=0., reduce_only=True, real_orders_enabled=False)
        return result
