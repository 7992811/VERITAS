"""Bounded closed-trade postmortem for owner review.

This module is diagnostic only. It never mutates canonical trading policy and
never promotes a hypothesis automatically. It turns already-durable trade,
decision and diagnostic evidence into a compact review object for the dashboard.
"""
from __future__ import annotations

import hashlib
import json
import math

import veritas_canonical_constitution as CTC

VERSION = "TRADE_POSTMORTEM_V2"


def _num(value):
    try:
        x = float(value)
        return x if math.isfinite(x) else None
    except (TypeError, ValueError):
        return None


def _obj(value):
    if isinstance(value, dict):
        return dict(value)
    if not value:
        return {}
    try:
        parsed = json.loads(value)
        return dict(parsed) if isinstance(parsed, dict) else {}
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}


def _copy(value):
    try:
        return json.loads(json.dumps(value, ensure_ascii=False, allow_nan=False, default=str))
    except (TypeError, ValueError):
        return None


def _first(*values):
    for value in values:
        if value not in (None, "", {}, []):
            return value
    return None


def _profit_threshold_pct():
    policy = getattr(CTC, "TREND_ACCELERATION_POLICY", {}) or {}
    protection = policy.get("profit_protection") or {}
    return float(protection.get("mfe_activation_pct_points") or 0.15)


def _proposal(trade_id, kind, title, rationale, *, conflicts=(), tests=(), blockers=()):
    key = hashlib.sha256((str(trade_id) + "|" + str(kind)).encode()).hexdigest()[:16]
    return {
        "candidate_id": "POST_" + key,
        "kind": kind,
        "title": title,
        "rationale": rationale,
        "status": "OWNER_REVIEW_REQUIRED",
        "automatic_promotion_allowed": False,
        "canonical_conflicts": list(conflicts),
        "shadow_tests": list(tests),
        "promotion_blockers": list(blockers),
    }


def entry_snapshot(trade, decision_payload=None, setup_payload=None, diagnosis=None, raw_payload=None):
    """Extract only bounded entry-time evidence needed by the review UI."""
    t = dict(trade or {})
    p = _obj(raw_payload if raw_payload is not None else t.get("payload"))
    d = _obj(decision_payload)
    s = _obj(setup_payload)
    plan = _obj(d.get("trade_plan"))
    features = _obj(d.get("features"))
    ctx = _obj(d.get("timeframe_entry_context") or plan.get("timeframe_entry_context"))
    event = _obj(
        p.get("entry_event_snapshot")
        or _obj(p.get("timeframe_entry_context")).get("event")
        or ctx.get("event")
    )
    norm = _obj((diagnosis or {}).get("normalization"))
    atr = _num(_first(norm.get("entry_atr"), event.get("atr"), p.get("entry_atr"), plan.get("atr")))
    entry = _num(_first(norm.get("original_entry_price"), t.get("avg_entry_price")))
    initial_stop = _num(_first(norm.get("initial_stop_price"), p.get("initial_stop_price"),
                               event.get("stop_price"), t.get("stop_price"), s.get("stop_price")))
    target = _num(_first(event.get("target_price"), p.get("initial_take_price"),
                         t.get("take_price"), plan.get("target_price"), s.get("target_price")))
    anchor = _num(_first(event.get("stop_anchor"), p.get("protected_stop_anchor")))
    direction = str(t.get("direction") or event.get("direction") or "")
    ladder = p.get("initial_target_ladder") or event.get("target_ladder") or p.get("active_target_ladder") or []
    if not isinstance(ladder, list):
        ladder = []
    ladder = [{
        "price": _num(step.get("price")),
        "fraction": _num(step.get("fraction")),
        "kind": step.get("kind"),
    } for step in ladder[:4] if isinstance(step, dict)]

    daily_ma = _obj(d.get("daily_ma") or plan.get("daily_ma"))
    moving_averages = {}
    for key in ("sma18", "sma50", "sma200"):
        value = _num(_first(daily_ma.get(key), features.get(key), d.get(key), plan.get(key)))
        if value is not None:
            moving_averages[key] = value
    ma_in_path = []
    if entry is not None and target is not None:
        lo, hi = min(entry, target), max(entry, target)
        for key, value in moving_averages.items():
            if lo <= value <= hi:
                ma_in_path.append({"name": key.upper(), "price": value})

    indicators = {}
    wanted = (
        "rsi", "macd", "adx", "atr", "relative_volume", "volume_ratio",
        "volatility", "realized_volatility", "sma18", "sma50", "sma200",
    )
    for source in (features, d, plan):
        if not isinstance(source, dict):
            continue
        for key, value in source.items():
            lk = str(key).lower()
            if any(lk == name or name in lk for name in wanted):
                if isinstance(value, (int, float, str, bool)) or value is None:
                    indicators.setdefault(str(key), value)
            if len(indicators) >= 20:
                break

    stop_anchor_buffer_atr = (
        abs(initial_stop - anchor) / atr
        if initial_stop is not None and anchor is not None and atr not in (None, 0)
        else None
    )
    target_distance_atr = (
        abs(target - entry) / atr
        if target is not None and entry is not None and atr not in (None, 0)
        else None
    )
    gross_rr = (
        abs(target - entry) / abs(entry - initial_stop)
        if target is not None and entry is not None and initial_stop is not None
        and abs(entry - initial_stop) > 1e-12
        else None
    )
    protected_kind = "previous_low" if direction == "LONG" else "previous_high" if direction == "SHORT" else None
    return _copy({
        "basis": "IMMUTABLE_ENTRY_EVENT_PLUS_NEAREST_DURABLE_DECISION",
        "entry": entry,
        "initial_stop": initial_stop,
        "initial_target": target,
        "atr": atr,
        "initial_risk_atr": _num(norm.get("initial_risk_atr")),
        "stop_anchor": anchor,
        "protected_level_kind": protected_kind,
        "stop_anchor_buffer_atr": stop_anchor_buffer_atr,
        "target_distance_atr": target_distance_atr,
        "gross_target_to_risk": gross_rr,
        "target_ladder": ladder,
        "trigger_level": _num(event.get("trigger_level")),
        "trigger_timeframe": _first(event.get("trigger_timeframe"), t.get("horizon")),
        "structural_timeframe": _first(event.get("structural_timeframe"), norm.get("structural_timeframe")),
        "stop_timeframe": _first(event.get("stop_timeframe"), norm.get("structural_timeframe"), t.get("horizon")),
        "atr_timeframe": _first(event.get("atr_timeframe"), norm.get("atr_timeframe"), t.get("horizon")),
        "target_timeframe": _first(event.get("target_timeframe"), norm.get("target_timeframe")),
        "regime": _first(p.get("regime"), p.get("entry_regime"), d.get("regime"), features.get("regime")),
        "horizon_structure": d.get("horizon_structure"),
        "daily_ma": daily_ma,
        "moving_averages": moving_averages,
        "moving_averages_in_trade_path": ma_in_path,
        "indicators": indicators,
        "entry_event_id": event.get("event_id"),
    })


def review(trade, diagnosis=None, snapshot=None, raw_payload=None):
    """Return proposal-only postmortem; no runtime policy is changed here."""
    t = dict(trade or {})
    p = _obj(raw_payload if raw_payload is not None else t.get("payload"))
    d = dict(diagnosis or {})
    snap = dict(snapshot or {})
    net = _num(t.get("net_pnl_rub"))
    gross = _num(t.get("gross_pnl_rub"))
    mfe = _num(t.get("mfe_pct"))
    mae = _num(t.get("mae_pct"))
    giveback = _num(t.get("giveback_pct"))
    threshold = _profit_threshold_pct()
    entry_fills = int(_num(t.get("entry_fill_count")) or 0)
    exit_fills = int(_num(t.get("exit_fill_count")) or 0)
    tp_fills = int(_num(t.get("take_profit_fill_count")) or 0)
    had_adds = entry_fills > 1
    target_stage = p.get("active_target_stage")
    partial_profit = bool(
        p.get("r17_tp1_done")
        or tp_fills > 0
        or (isinstance(target_stage, int) and not isinstance(target_stage, bool) and target_stage > 0)
        or str(p.get("last_target_kind") or "").upper() in ("PARTIAL", "TP1")
    )
    protection_active = bool(
        p.get("r_accel_mfe_profit_lock_active")
        or p.get("r55_net_profit_lock_active")
        or _obj(p.get("net_profit_protection")).get("state") == "PROTECTED"
    )
    evidence_status = str(d.get("status") or "UNVERIFIED")
    violations = [
        str(v.get("code")) for v in (d.get("violations") or [])
        if isinstance(v, dict) and v.get("code")
    ]
    limitations = [str(x) for x in (d.get("evidence_limitations") or []) if x]
    if d.get("exclusion_reason"):
        limitations.append(str(d["exclusion_reason"]))
    limitations = sorted(set(limitations))

    if evidence_status == "RULE_VIOLATION":
        classification = "PROVEN_RULE_VIOLATION"
    elif net is not None and net <= 0 and mfe is not None and mfe >= threshold:
        classification = "PROFIT_GIVEBACK_REVIEW"
    elif net is not None and net < 0 and (mfe is None or mfe < threshold):
        classification = "ENTRY_OR_THESIS_REVIEW"
    elif net is not None and net > 0:
        classification = "PROFITABLE_REFERENCE"
    else:
        classification = "OUTCOME_REVIEW"

    issues, strengths, proposals = [], [], []
    if net is not None and net < 0:
        issues.append("Итог сделки после расходов отрицательный.")
    elif net is not None and net > 0:
        strengths.append("Итог сделки после расходов положительный.")
    if violations:
        issues.append("Есть доказанные нарушения сохранённых правил: " + ", ".join(violations) + ".")
        proposals.append(_proposal(
            t.get("trade_id"), "PROVEN_RULE_REPAIR",
            "Исправить доказанное нарушение правила",
            "Изменение разрешается только для конкретного доказанного нарушения; остальные канонические ограничения сохраняются.",
            conflicts=violations,
            tests=({"test": "REPLAY_SAME_EPISODE_WITH_RULE_COMPLIANCE"},),
        ))

    material_giveback = bool(net is not None and net <= 0 and mfe is not None and mfe >= threshold)
    if material_giveback:
        issues.append(
            f"MFE достигал {mfe:.3f}% при пороге {threshold:.2f}%, но итог после расходов не положительный."
        )
        blockers = () if evidence_status == "VERIFIED_RULE_OUTCOME" else ("ORDERED_PATH_REPLAY_OR_FUTURE_OOS_REQUIRED",)
        proposals.append(_proposal(
            t.get("trade_id"), "SUSTAINED_PROFIT_PROTECTION_REPLAY",
            "Проверить адаптивную защиту устойчивой прибыли",
            "Сравнить фактическое сопровождение с текущим правилом MFE 0,15% + выдержка по риск-таймфрейму + net-positive stop после расходов.",
            conflicts=(
                "Не переводить стоп в безубыток по краткому ценовому всплеску.",
                "Подтверждённый структурный swing исходного риск-ТФ остаётся отдельным механизмом уменьшения риска.",
            ),
            tests=(
                {"test": "ORDERED_PATH_REPLAY_CURRENT_015_PROTECTION"},
                {"test": "FUTURE_OOS_CAPTURE_AND_FALSE_STOP_RATE"},
            ),
            blockers=blockers,
        ))

    if had_adds:
        strengths.append(f"Зафиксировано {entry_fills} входных исполнений: присутствуют доборы.")
    if had_adds and (partial_profit or protection_active) and net is not None and net < 0:
        issues.append("После частичной фиксации/защиты прибыли и доборов весь торговый эпизод завершился в минус.")
        proposals.append(_proposal(
            t.get("trade_id"), "EPISODE_ADD_PROFIT_FLOOR",
            "Защитить результат всей идеи при последующих доборах",
            "Перед новым ADD после уже заработанной прибыли проверить projected net всей позиции на действующем стопе с учётом новой комиссии/проскальзывания. Добор не должен превращать защищённый эпизод в отрицательный.",
            conflicts=(
                "Сохранить трендовое ускорение и право на подтверждённый ADD.",
                "Не расширять действующий структурный/защитный стоп ради нового добора.",
            ),
            tests=(
                {"test": "PRE_ADD_WHOLE_EPISODE_NET_AT_STOP"},
                {"test": "BRENT_ETH_REPLAY_WITH_EXISTING_TARGET_REPLAN"},
            ),
            blockers=("SHADOW_AND_OOS_REQUIRED",),
        ))

    if gross is not None and gross > 0 and net is not None and net <= 0:
        issues.append("Валовая прибыль была положительной, но расходы перевели итог в минус.")
        proposals.append(_proposal(
            t.get("trade_id"), "COST_DRAG_REVIEW",
            "Проверить экономику частичных выходов и доборов",
            "Сократить churn только если replay показывает улучшение net-результата после всех комиссий и проскальзывания.",
            conflicts=("Не отменять структурно необходимые выходы ради экономии комиссии.",),
            tests=({"test": "FILL_LEDGER_COST_ATTRIBUTION"},),
        ))

    risk_atr = _num(snap.get("initial_risk_atr"))
    if risk_atr is not None and risk_atr > 4.0 and net is not None and net < 0:
        issues.append(f"Исходный риск составлял {risk_atr:.2f} ATR риск-таймфрейма.")
        proposals.append(_proposal(
            t.get("trade_id"), "STOP_VOLATILITY_REVIEW",
            "Проверить слишком широкий риск-контур",
            "Не приближать структурный стоп искусственно. Проверить, должен ли широкий stop-ATR уменьшать размер или требовать отдельного подтверждения risk-context.",
            conflicts=(
                "Стоп остаётся за подтверждённым high/low.",
                "Размер позиции подстраивается под структурный риск, а не наоборот.",
            ),
            tests=({"test": "STOP_ATR_BUCKET_OOS", "values": [3.0, 4.0, 5.0, 6.0]},),
        ))

    ma_path = list(snap.get("moving_averages_in_trade_path") or [])
    if ma_path:
        issues.append("На траектории вход→цель находились MA: " + ", ".join(x["name"] for x in ma_path if x.get("name")) + ".")
    elif snap.get("moving_averages"):
        strengths.append("Сохранённые MA не находились между входом и исходной целью.")

    if snap.get("stop_anchor") is not None and snap.get("initial_stop") is not None:
        direction = str(t.get("direction") or "")
        anchor_ok = (
            float(snap["initial_stop"]) < float(snap["stop_anchor"]) if direction == "LONG"
            else float(snap["initial_stop"]) > float(snap["stop_anchor"]) if direction == "SHORT"
            else None
        )
        if anchor_ok:
            strengths.append("Исходный стоп расположен за защищаемым предыдущим high/low.")
        elif anchor_ok is False:
            issues.append("Исходный стоп не расположен за сохранённым защищаемым high/low.")

    return _copy({
        "version": VERSION,
        "trade_id": t.get("trade_id"),
        "episode_key": t.get("episode_key"),
        "classification": classification,
        "evidence_status": evidence_status,
        "learning_eligible": bool(d.get("learning_eligible")),
        "material_mfe_threshold_pct": threshold,
        "financial_result_rub": net,
        "gross_result_rub": gross,
        "path": {
            "mfe_pct": mfe,
            "mae_pct": mae,
            "giveback_pct": giveback,
            "material_profit_giveback": material_giveback,
        },
        "execution": {
            "entry_fill_count": entry_fills,
            "exit_fill_count": exit_fills,
            "take_profit_fill_count": tp_fills,
            "had_adds": had_adds,
            "partial_profit_observed": partial_profit,
            "profit_protection_was_active": protection_active,
        },
        "levels_volatility": {
            key: snap.get(key) for key in (
                "entry", "initial_stop", "initial_target", "atr", "initial_risk_atr",
                "stop_anchor", "protected_level_kind", "stop_anchor_buffer_atr",
                "target_distance_atr", "gross_target_to_risk", "target_ladder", "trigger_level",
            )
        },
        "market_context": {
            key: snap.get(key) for key in (
                "regime", "trigger_timeframe", "structural_timeframe", "stop_timeframe",
                "atr_timeframe", "target_timeframe", "horizon_structure", "daily_ma",
                "moving_averages", "moving_averages_in_trade_path", "indicators",
            )
        },
        "diagnostic_attribution": d.get("primary_attribution"),
        "violations": violations,
        "evidence_limitations": limitations,
        "strengths": strengths,
        "issues": issues,
        "proposals": proposals,
        "governance": {
            "automatic_rule_change": False,
            "owner_approval_required": True,
            "parameter_changes": "SHADOW_ONLY_UNTIL_APPROVED_AND_VALIDATED",
            "promotion_requirements": list((CTC.LEARNING_POLICY or {}).get("promotion_requires") or ()),
        },
    })
