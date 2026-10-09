"""Causal post-trade diagnosis, separate from the financial ledger and orders.

A losing outcome cannot prove that the trigger or stop was wrong. Rule verdicts
require the immutable entry event, its source, timestamps and recorded geometry.
Excursions use the original entry fill and stop, never a later average/trailed stop.
Management observations are research hypotheses, not automatic parameter changes.
"""
from __future__ import annotations

import math

import veritas_price_source as SOURCE
import veritas_timeframe_structure as STRUCTURE
import veritas_trade_audit as AUDIT


VERSION = "STRUCTURAL_TRADE_DIAGNOSTICS_V2"
_NUMERIC_POLICY = tuple(STRUCTURE.DEFAULT_POLICY)


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _object(value):
    return value if isinstance(value, dict) else {}


def _positive(value):
    value = _number(value)
    return value if value is not None and value > 0 else None


def _same_number(left, right):
    return (left is not None and right is not None
            and math.isclose(left, right, rel_tol=1e-9, abs_tol=1e-10))


def _event_evidence(trade, p):
    """Validate a recorded event independently of whether entry respected it.

    An entry before a valid event's confirmation is a provable timing violation;
    missing confirmation or internally inconsistent timestamps remain unverified.
    No timestamp, timeframe, source, ATR or policy value is filled in from today.
    """
    event = _object(p.get("entry_event_snapshot"))
    source = _object(p.get("price_source_lock") or p.get("entry_execution_source_identity"))
    actual = _object(event.get("source_identity"))
    event_id = event.get("event_id")
    declared = p.get("idea_event_id") or p.get("r66_event_id")
    if (not isinstance(event_id, str) or not event_id or event_id.startswith("R79_SIG_")
            or (declared and event_id != declared)):
        return None, "UNVERIFIED_EVENT_ID"
    if (not source.get("key") or source.get("asset") != trade.get("asset")
            or actual.get("asset") != trade.get("asset")
            or not SOURCE.same(source, actual)):
        return None, "UNVERIFIED_EVENT_SOURCE"
    if event.get("event_type") == "VERIFIED_QUOTE_STRUCTURAL_BREAKOUT":
        import veritas_structural_breakout as SB
        if not AUDIT.quote_event_evidence(trade, event):
            return None, "UNVERIFIED_QUOTE_BREAKOUT_PROOF"
        values = {key: _positive(event.get(key)) for key in
                  ("trigger_level", "signal_price", "stop_anchor", "stop_price", "target_price", "atr")}
        if any(value is None for value in values.values()):
            return None, "MISSING_EVENT_GEOMETRY"
        times = {key: AUDIT._timestamp(event.get(key)) for key in ("signal_at", "confirmed_at")}
        if any(value is None for value in times.values()):
            return None, "MISSING_EVENT_TIME_EVIDENCE"
        return dict(event=event, source=source, timeframe=event["timeframe"],
                    risk_timeframe=event["structural_timeframe"], quote_breakout=True,
                    policy=SB._policy(event["policy"]), times=times, values=values,
                    sign=1 if event["direction"] == "LONG" else -1), None
    timeframe = event.get("timeframe")
    if (event.get("version") != STRUCTURE.VERSION
            or event.get("event_type") not in ("SAME_TIMEFRAME_STRUCTURAL_BREAKOUT", "DAILY_MA_REBOUND")
            or not isinstance(timeframe, str) or timeframe not in STRUCTURE.TIMEFRAMES
            or event.get("asset") != trade.get("asset")
            or event.get("direction") not in ("LONG", "SHORT")
            or event.get("confirmation") != "CLOSED_" + timeframe + "_BAR"):
        return None, "UNVERIFIED_EVENT_PROVENANCE"
    if any(not isinstance(event.get(k), str) or event[k] not in STRUCTURE.TIMEFRAMES
           for k in ("atr_timeframe", "stop_timeframe", "target_timeframe")):
        return None, "MISSING_EVENT_TIMEFRAME"
    policy = _object(event.get("policy"))
    if any(_number(policy.get(k)) is None for k in _NUMERIC_POLICY):
        return None, "MISSING_IMMUTABLE_EVENT_POLICY"
    try:
        checked_policy = STRUCTURE._policy(policy)
    except (TypeError, ValueError, OverflowError):
        return None, "INVALID_IMMUTABLE_EVENT_POLICY"
    times = {key: AUDIT._timestamp(event.get(key)) for key in
             ("breakout_bar_at", "signal_at", "confirmed_at", "trigger_pivot_at",
              "stop_pivot_at", "level_available_at", "stop_level_available_at", "atr_observed_until")}
    if any(value is None for value in times.values()):
        return None, "MISSING_EVENT_TIME_EVIDENCE"
    opening, signal, confirmed = (times[k] for k in ("breakout_bar_at", "signal_at", "confirmed_at"))
    seconds = STRUCTURE.TIMEFRAMES[timeframe]
    if (not opening + seconds <= signal <= confirmed
            or not times["trigger_pivot_at"] < times["level_available_at"] <= opening
            or not times["stop_pivot_at"] < times["stop_level_available_at"] <= opening
            or times["atr_observed_until"] > opening):
        return None, "NONCAUSAL_EVENT_EVIDENCE"
    values = {key: _positive(event.get(key)) for key in
              ("trigger_level", "signal_price", "stop_anchor", "stop_price", "target_price", "atr")}
    if any(value is None for value in values.values()):
        return None, "MISSING_EVENT_GEOMETRY"
    sign = 1 if event["direction"] == "LONG" else -1
    if (sign * (values["signal_price"] - values["trigger_level"]) <= 0
            or sign * (values["trigger_level"] - values["stop_anchor"]) <= 0
            or sign * (values["target_price"] - values["trigger_level"]) <= 0):
        return None, "INCONSISTENT_EVENT_GEOMETRY"
    if event.get("event_type") == "DAILY_MA_REBOUND":
        from veritas_ma_rebound import validate_event
        if not validate_event(event, source).get("eligible"):
            return None, "UNVERIFIED_DAILY_MA_PROOF"
    elif event.get("ma_proof") or event_id.startswith("MAR_") or event.get("ma_rebound_version"):
        return None, "MISLABELLED_DAILY_MA_EVENT"
    return dict(event=event, source=source, timeframe=timeframe, policy=checked_policy,
                times=times, values=values, sign=sign), None


def diagnose(trade, *, additional_exclusion=None):
    """Return evidence-backed diagnosis without mutating the trade or its P/L.

    ``outcome_evidence_eligible`` means the net closed-trade outcome is proved
    by the immutable event/source/fill/initial-risk/accounting evidence.
    ``learning_eligible`` remains the stricter path-aware strategy-quality tier.
    Missing path coverage never becomes evidence of MFE/MAE, a correct exit or
    profit left on the table. Proven implementation violations remain separate.
    """
    t = dict(trade or {})
    p = AUDIT.payload(t.get("payload"))
    net, gross = _number(t.get("net_pnl_rub")), _number(t.get("gross_pnl_rub"))
    outcome = "UNKNOWN" if net is None else "LOSS" if net < 0 else "PROFIT" if net > 0 else "FLAT"
    result = dict(version=VERSION, status="UNVERIFIED", outcome=outcome,
        primary_attribution="UNVERIFIED_TRADE_EVIDENCE", attributions=["UNVERIFIED_TRADE_EVIDENCE"],
        learning_action="REVIEW_ORIGINAL_EVIDENCE", learning_eligible=False,
        outcome_evidence_eligible=False, path_evidence_eligible=False,
        strategy_quality_eligible=False, rule_evidence_eligible=False,
        directional_error=False, violations=[], hypotheses=[], normalization={},
        rule_scope="RECORDED_STRUCTURAL_ENTRY_AND_INITIAL_STOP_POLICY_ONLY",
        subsequent_management_rules_verified=False,
        parameter_changes_applied=False, financial_columns_changed=False)
    audit_problem = AUDIT.evidence_exclusion(t)
    # Event-rule discrepancies are meaningful only on trusted original data.
    # UNVERIFIED_EVENT may specifically be a recorded early entry/wrong TF, which
    # is checked below. Source contamination and recovered/admin records cannot
    # be promoted to causal price/stop evidence even if their numbers disagree.
    if audit_problem not in (None, "UNVERIFIED_EVENT"):
        result["exclusion_reason"] = audit_problem
        return result
    source_problem = AUDIT._source_exclusion(t, p)
    evidence, event_problem = _event_evidence(t, p)
    if source_problem or event_problem:
        result["exclusion_reason"] = source_problem or event_problem
        return result
    e, values, policy = evidence["event"], evidence["values"], evidence["policy"]
    timeframe, sign = evidence["timeframe"], evidence["sign"]
    quote_breakout = evidence.get("quote_breakout") is True
    risk_timeframe = evidence.get("risk_timeframe", timeframe)
    if quote_breakout:
        result["rule_scope"] = "RECORDED_QUOTE_BREAKOUT_PROTECTED_PARENT_AND_HISTORICAL_TARGETS"
    entered = AUDIT._timestamp(t.get("opened_at"))
    fill = _object(p.get("entry_execution_model"))
    entry, initial_stop = _positive(fill.get("fill_price")), _positive(p.get("initial_stop_price"))
    missing = []
    if entered is None:
        missing.append("MISSING_ACTUAL_ENTRY_TIME")
    direction = t.get("direction")
    expected_side = "BUY" if direction == "LONG" else "SELL_SHORT" if direction == "SHORT" else None
    if (entry is None or fill.get("asset") != t.get("asset")
            or expected_side is None or fill.get("side") != expected_side):
        entry = None
        missing.append("MISSING_IMMUTABLE_FIRST_FILL")
    if initial_stop is None:
        missing.append("MISSING_IMMUTABLE_INITIAL_STOP")
    violations = result["violations"]

    def violation(code, observed, required):
        violations.append(dict(code=code, observed=observed, required=required))

    if t.get("direction") not in ("LONG", "SHORT"):
        missing.append("MISSING_TRADE_DIRECTION")
    elif t.get("direction") != e["direction"]:
        violation("ENTRY_EVENT_DIRECTION_MISMATCH", t["direction"], e["direction"])
    if not isinstance(t.get("horizon"), str) or t.get("horizon") not in STRUCTURE.TIMEFRAMES:
        missing.append("MISSING_ENTRY_TIMEFRAME")
    elif t.get("horizon") != timeframe:
        violation("ENTRY_TIMEFRAME_MISMATCH", t["horizon"], timeframe)
    for field in ("atr_timeframe", "stop_timeframe", "target_timeframe"):
        expected_timeframe = ("HISTORICAL_ZONES" if field == "target_timeframe" else risk_timeframe) if quote_breakout else timeframe
        if e[field] != expected_timeframe:
            violation(field.upper() + "_MISMATCH", e[field], expected_timeframe)
        declared = p.get(field)
        if declared is not None and declared != expected_timeframe:
            violation("RECORDED_" + field.upper() + "_MISMATCH", declared, expected_timeframe)
    for field in ("execution_timeframe", "execution_horizon"):
        if p.get(field) is not None and p[field] != timeframe:
            violation("RECORDED_ENTRY_TIMEFRAME_MISMATCH", p[field], timeframe)
    if policy["atr_period"] != 20:
        missing.append("ENTRY_ATR20_UNVERIFIED")
    atr_consistent = e["atr_timeframe"] == risk_timeframe and policy["atr_period"] == 20
    recorded_atr = _positive(p.get("entry_atr"))
    if recorded_atr is None:
        missing.append("MISSING_IMMUTABLE_ENTRY_ATR")
    elif atr_consistent and not _same_number(recorded_atr, values["atr"]):
        violation("RECORDED_ENTRY_ATR_MISMATCH", recorded_atr, values["atr"])
    expected_stop = None
    if atr_consistent and e["stop_timeframe"] == risk_timeframe:
        expected_stop = values["stop_anchor"] - sign * policy["stop_buffer_atr"] * values["atr"]
        if not _same_number(values["stop_price"], expected_stop):
            violation("EVENT_STOP_OUTSIDE_RECORDED_RULE", values["stop_price"], expected_stop)
        if initial_stop is not None and not _same_number(initial_stop, expected_stop):
            violation("INITIAL_STOP_OUTSIDE_RECORDED_RULE", initial_stop, expected_stop)
        if not quote_breakout and e["target_timeframe"] == timeframe:
            expected_target = values["trigger_level"] + sign * max(
                policy["target_r_multiple"] * sign * (values["trigger_level"] - expected_stop),
                policy["min_target_atr"] * values["atr"])
            if not _same_number(values["target_price"], expected_target):
                violation("EVENT_TARGET_OUTSIDE_RECORDED_RULE", values["target_price"], expected_target)
    if entered is not None:
        confirmed, signal = evidence["times"]["confirmed_at"], evidence["times"]["signal_at"]
        if entered < confirmed:
            violation("ENTRY_BEFORE_EVENT_CONFIRMATION", entered, confirmed)
        else:
            max_age = policy["max_signal_age_bars"] * STRUCTURE.TIMEFRAMES[timeframe]
            if quote_breakout:
                max_age = max(max_age, policy["minimum_entry_window_seconds"])
            if entered - signal > max_age:
                violation("ENTRY_AFTER_EVENT_EXPIRY", entered - signal, max_age)
        spent_at = AUDIT._timestamp(e.get("spent_at"))
        if (e.get("spent") is True and spent_at is not None
                and confirmed <= spent_at <= entered):
            violation("ENTRY_AFTER_EVENT_SPENT", entered, spent_at)
        elif e.get("spent") is not False or e.get("spent_at") is not None:
            missing.append("UNVERIFIED_EVENT_SPENT_STATE")
    geometry = dict(timeframe=timeframe, original_entry_price=entry, initial_stop_price=initial_stop,
                    entry_atr=values["atr"] if atr_consistent else None, atr_period=policy["atr_period"],
                    basis="IMMUTABLE_FIRST_FILL_AND_INITIAL_STOP", initial_risk_price=None,
                    initial_risk_pct=None, initial_risk_atr=None, mfe_r=None, mae_r=None,
                    mfe_atr=None, mae_atr=None, final_exit_r=None, final_exit_atr=None,
                    monetary_net_r=None, monetary_net_r_status="REQUIRES_ACTUAL_ORIGINAL_UNITS",
                    path_status="UNVERIFIED")
    if quote_breakout:
        geometry.update(trigger_timeframe=e["trigger_timeframe"], structural_timeframe=risk_timeframe,
                        atr_timeframe=e["atr_timeframe"], target_timeframe=e["target_timeframe"])
    result["normalization"] = geometry
    if entered is not None:
        times = evidence["times"]
        seconds = STRUCTURE.TIMEFRAMES[timeframe]
        geometry["timing"] = dict(time_basis="UTC_EPOCH_SECONDS", opened_at=entered,
            signal_at=times["signal_at"], confirmed_at=times["confirmed_at"],
            delay_seconds=entered-times["confirmed_at"],
            delay_bars=(entered-times["confirmed_at"])/seconds,
            signal_to_entry_seconds=entered-times["signal_at"],
            signal_to_entry_bars=(entered-times["signal_at"])/seconds,
            confirmation_availability_delay_seconds=times["confirmed_at"]-times["signal_at"],
            effect="DIAGNOSTIC_ONLY_NO_NEW_ENTRY_VETO")
    actual_sign = 1 if direction == "LONG" else -1 if direction == "SHORT" else None
    risk = (actual_sign * (entry - initial_stop)
            if actual_sign is not None and entry is not None and initial_stop is not None else None)
    if risk is not None and risk <= 0:
        violation("INITIAL_STOP_ON_WRONG_SIDE_OF_ENTRY", initial_stop,
                  "BELOW_ENTRY" if actual_sign > 0 else "ABOVE_ENTRY")
        risk = None
    if risk is not None:
        geometry.update(initial_risk_price=risk, initial_risk_pct=100. * risk / entry,
                        initial_risk_atr=risk / values["atr"] if atr_consistent else None)
    if entry is not None and atr_consistent and t.get("direction") == e["direction"]:
        extension = sign * (entry - values["trigger_level"]) / values["atr"]
        geometry["entry_extension_atr"] = extension
        if extension < 0:
            violation("ENTRY_LEVEL_NOT_HELD", extension, 0.)
        elif quote_breakout:
            progress = sign * (entry-values["trigger_level"]) / (sign*(values["target_price"]-values["trigger_level"]))
            geometry["target_progress"] = progress
            if progress > policy["max_target_progress"]:
                violation("ENTRY_EXCEEDS_TARGET_PROGRESS", progress, policy["max_target_progress"])
        elif extension > policy["max_extension_atr"]:
            violation("ENTRY_EXCEEDS_EVENT_EXTENSION", extension, policy["max_extension_atr"])
        if risk is not None and risk / values["atr"] > policy["max_stop_atr"]:
            violation("INITIAL_RISK_EXCEEDS_EVENT_POLICY", risk / values["atr"], policy["max_stop_atr"])

    from veritas_observation_path import assessment
    path_check = assessment(t)
    result["path_assessment"] = path_check
    path_problem = None if path_check.get("eligible") else (
        path_check.get("reason") or path_check.get("exclusion_reason") or "INCOMPLETE_OBSERVED_PATH")
    path_limitations = [path_problem] if path_problem else []
    if additional_exclusion:
        missing.append(additional_exclusion)
    if any(_number(t.get(k)) is None for k in ("gross_pnl_rub", "fees_rub", "funding_rub", "net_pnl_rub")):
        missing.append("INCOMPLETE_ACCOUNTING")
    opened, closed = entered, AUDIT._timestamp(t.get("closed_at"))
    if (str(t.get("status") or "").upper() not in ("CLOSED", "CLOSE", "EXITED")
            or opened is None or closed is None or closed < opened):
        missing.append("INCOMPLETE_CLOSED_TRADE")
    # A path witness cannot substitute for the immutable fill/stop/ATR of entry.
    path = _object(p.get("observation_path"))
    path_consistent = path_check.get("eligible") and entry is not None and risk is not None and atr_consistent
    if path_consistent and not all(_same_number(_positive(path.get(k)), value) for k, value in (
            ("original_entry_price", entry), ("initial_stop_price", initial_stop), ("entry_atr", values["atr"]))):
        path_consistent = False
        path_limitations.append("PATH_INITIAL_GEOMETRY_MISMATCH")
    if path_consistent:
        low, high = _positive(path.get("min_price")), _positive(path.get("max_price"))
        if low is None or high is None or low > high:
            path_limitations.append("INVALID_OBSERVED_PATH_EXTREMES")
        else:
            favorable = max(0., (high - entry) if actual_sign > 0 else (entry - low))
            adverse = min(0., (low - entry) if actual_sign > 0 else (entry - high))
            geometry.update(path_status="OBSERVED_FIRST_ENTRY_BASIS", mfe_r=favorable / risk,
                            mae_r=adverse / risk, mfe_atr=favorable / values["atr"],
                            mae_atr=adverse / values["atr"], mfe_pct=100. * favorable / entry,
                            mae_pct=100. * adverse / entry)
            # avg_exit_price is the final fill, not a weighted multi-fill result.
            last_exit = _positive(_object(p.get("last_exit_execution_model")).get("fill_price"))
            if last_exit is not None:
                geometry.update(final_exit_r=actual_sign * (last_exit - entry) / risk,
                                final_exit_atr=actual_sign * (last_exit - entry) / values["atr"],
                                final_exit_basis="LAST_EXIT_FILL_ONLY_NOT_TOTAL_PNL")
    if violations and (entered is None or entry is None):
        result["unverified_discrepancies"] = list(violations)
        result["violations"] = []
        missing.append("MISSING_ORIGINAL_EXECUTION_PROOF")
    elif violations:
        result.update(status="RULE_VIOLATION", primary_attribution="PROVEN_ENTRY_RULE_VIOLATION",
                      attributions=["PROVEN_ENTRY_RULE_VIOLATION"] + [v["code"] for v in violations],
                      learning_action="REPAIR_PROVEN_RULE_VIOLATION", rule_evidence_eligible=True,
                      exclusion_reason="PROVEN_RULE_VIOLATION", evidence_limitations=sorted(set(missing)))
        if all("STOP" in v["code"] or "ATR" in v["code"] for v in violations):
            result["primary_attribution"] = "PROVEN_STOP_OR_ATR_RULE_VIOLATION"
            result["attributions"][0] = result["primary_attribution"]
        return result
    if audit_problem:
        missing.append(audit_problem)
    if missing:
        result.update(exclusion_reason=missing[0], evidence_limitations=sorted(set(missing)))
        return result

    reason = str(p.get("exit_reason") or p.get("close_reason") or t.get("exit_reason") or "").upper()
    primary = ("VALID_STRUCTURAL_STOP_LOSS" if net < 0 and "STOP" in reason else
               "VALID_LOSING_TRADE" if net < 0 else "VALID_PROFITABLE_TRADE" if net > 0 else "VALID_FLAT_TRADE")
    attrs = [primary]
    if gross > 0 and net <= 0:
        attrs.append("COST_DRAG")

    # The net outcome is independently proved even when the sampled quote path
    # is incomplete.  Keep path/capture learning fail-closed while allowing
    # size/calibration learning to consume the actual cash outcome.
    result["outcome_evidence_eligible"] = True
    if path_limitations:
        result.update(
            status="VERIFIED_OUTCOME_ONLY",
            learning_eligible=False,
            path_evidence_eligible=False,
            strategy_quality_eligible=False,
            exclusion_reason=path_limitations[0],
            evidence_limitations=sorted(set(path_limitations)),
            learning_action="COUNT_NET_OUTCOME_ONLY",
            primary_attribution=primary,
            attributions=attrs,
        )
        return result

    result.update(status="VERIFIED_RULE_OUTCOME", learning_eligible=True,
                  outcome_evidence_eligible=True, path_evidence_eligible=True,
                  strategy_quality_eligible=True,
                  exclusion_reason=None, learning_action="COUNT_STRATEGY_OUTCOME")
    # At least one original risk unit was observed, then the final fill gave back
    # half a risk unit. This is a preregistered review trigger, not a stop verdict
    # or proof that the peak could have been captured with executable orders.
    mfe_r, final_r = geometry.get("mfe_r"), geometry.get("final_exit_r")
    if mfe_r is not None and final_r is not None and mfe_r >= 1. and mfe_r - final_r >= .5:
        result["hypotheses"].append(dict(code="PROFIT_CAPTURE_HYPOTHESIS", mfe_r=mfe_r,
            final_exit_r=final_r, giveback_to_final_exit_r=mfe_r - final_r,
            rule="COMPARE_WEIGHTED_PARTIAL_EXITS_AND_STRUCTURAL_RUNNER", automatic_action=False,
            limitation="OBSERVED_EXTREMES_ARE_NOT_EXECUTABLE_FILLS; FINAL_FILL_IS_NOT_WHOLE_TRADE_CAPTURE"))
        attrs.append("PROFIT_CAPTURE_HYPOTHESIS")
    result.update(primary_attribution=primary, attributions=attrs)
    return result


def conclusion(record):
    """Concise Russian diagnostic copy; never elevate a hypothesis to an error."""
    status = record.get("status")
    if status == "RULE_VIOLATION":
        return "Подтверждено нарушение сохранённого структурного правила входа или исходного стопа; проверяемое противоречие указано в доказательствах сделки."
    if status == "VERIFIED_OUTCOME_ONLY":
        return "Финансовый исход сделки подтверждён для обучения net outcome/размеру позиции, но непрерывный путь цены недостаточен для выводов о MFE/MAE, стопе или качестве выхода."
    if status != "VERIFIED_RULE_OUTCOME":
        return "Данных недостаточно для вывода о качестве правил; финансовый результат сохранён, диагноз требует исходных доказательств."
    if record.get("outcome") == "LOSS":
        return "Проверенные структурные правила входа и исходного стопа соблюдены; убыток учитывается в эффективности сценария и сам по себе не доказывает ошибку направления. Последующее сопровождение оценивается отдельно."
    return "Проверенные структурные правила входа и исходного стопа соблюдены; наблюдённый результат учитывается в эффективности сценария. Изменения последующего сопровождения требуют сравнительной проверки."
