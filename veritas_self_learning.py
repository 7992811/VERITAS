"""Closed-trade postmortem used by the Self-learning UI.

The module is diagnostic and proposal-only. It never mutates canonical policy.
"""
from __future__ import annotations
import hashlib, json, math
import veritas_canonical_constitution as CTC
import veritas_owner_policy as VOP

VERSION="CLOSED_TRADE_POSTMORTEM_V1"

def _num(v):
    try:
        x=float(v); return x if math.isfinite(x) else None
    except (TypeError,ValueError): return None

def _copy(v):
    try:return json.loads(json.dumps(v,ensure_ascii=False,allow_nan=False,default=str))
    except Exception:return None

def _first(*values):
    for v in values:
        if v not in (None,"",{},[]): return v
    return None

def entry_snapshot(decision_payload, trade_payload=None, setup_payload=None):
    d=decision_payload or {}; tp=trade_payload or {}; sp=setup_payload or {}
    plan=d.get("trade_plan") or {}
    features=d.get("features") or {}
    ctx=(d.get("timeframe_entry_context") or plan.get("timeframe_entry_context")
         or tp.get("timeframe_entry_context") or {})
    event=ctx.get("event") or {}
    protected=event.get("protected_swing") or {}
    indicators={}
    aliases=("rsi","macd","adx","atr","volume_ratio","relative_volume","volatility",
             "realized_volatility","sma18","sma50","sma200","ema18","ema50","ema200")
    for source in (features,d,plan):
        for k,v in (source.items() if isinstance(source,dict) else ()):
            lk=str(k).lower()
            if any(a==lk or a in lk for a in aliases):
                if isinstance(v,(str,int,float,bool)) or v is None:
                    indicators[k]=v
    targets=event.get("target_ladder") or []
    return _copy({
        "regime":_first(d.get("regime"),features.get("regime"),tp.get("regime"),sp.get("regime")),
        "trigger_timeframe":_first(event.get("trigger_timeframe"),d.get("horizon")),
        "structural_timeframe":event.get("structural_timeframe"),
        "stop_timeframe":event.get("stop_timeframe"),
        "atr_timeframe":event.get("atr_timeframe"),
        "target_timeframe":event.get("target_timeframe"),
        "trigger_level":_first(event.get("trigger_level"),plan.get("trigger_level")),
        "protected_swing":protected,
        "stop_anchor":_first(event.get("stop_anchor"),protected.get("price")),
        "atr":_first(event.get("atr"),ctx.get("atr"),plan.get("atr")),
        "initial_stop":_first(event.get("stop_price"),plan.get("stop_price"),sp.get("stop_price")),
        "initial_target":_first(event.get("target_price"),plan.get("target_price"),sp.get("target_price")),
        "target_ladder":targets,
        "daily_ma":d.get("daily_ma") or plan.get("daily_ma"),
        "daily_ma_periods":d.get("daily_ma_periods") or plan.get("daily_ma_periods"),
        "horizon_structure":d.get("horizon_structure"),
        "intraday_structure":d.get("intraday_structure"),
        "structural_levels":d.get("structural_levels") or plan.get("structural_levels"),
        "indicators":indicators,
        "entry_event_id":event.get("event_id"),
    })

def _proposal(trade_id, kind, title, rationale, conflicts=None, tests=None):
    rid="SL_"+hashlib.sha256((str(trade_id)+"|"+kind).encode()).hexdigest()[:16]
    return {"candidate_id":rid,"kind":kind,"title":title,"rationale":rationale,
            "status":"OWNER_REVIEW_REQUIRED","automatic_promotion_allowed":False,
            "canonical_conflicts":list(conflicts or []),"shadow_tests":list(tests or [])}

def review(trade, snapshot=None):
    t=dict(trade or {}); s=snapshot or {}
    entry=_num(t.get("avg_entry_price")); exitp=_num(t.get("avg_exit_price"))
    final_stop=_num(t.get("stop_price")); final_target=_num(t.get("take_price"))
    initial_stop=_num(s.get("initial_stop")) or final_stop
    initial_target=_num(s.get("initial_target")) or final_target
    stop=initial_stop; target=initial_target
    atr=_num(s.get("atr")); mfe=_num(t.get("mfe_pct")); mae=_num(t.get("mae_pct"))
    net=_num(t.get("net_pnl_rub")) or 0.0
    gross=_num(t.get("gross_pnl_rub")) or 0.0
    fees=max(0.0,_num(t.get("fees_rub")) or 0.0)
    funding=max(0.0,_num(t.get("funding_rub")) or 0.0)
    raw_payload=t.get("payload") or {}
    try:
        trade_payload=json.loads(raw_payload) if isinstance(raw_payload,str) else dict(raw_payload)
    except (TypeError,ValueError):
        trade_payload={}
    target_history=list(trade_payload.get("target_lifecycle_history") or [])
    partial_seen=any((x or {}).get("action")=="TARGET_PARTIAL" for x in target_history)
    seen_partial=False; replan_after_partial=False
    for item in target_history:
        action=str((item or {}).get("action") or "")
        if action=="TARGET_PARTIAL":
            seen_partial=True
        elif seen_partial and action=="CONFIRMED_ADD_REPLANS_REMAINING_TARGETS":
            replan_after_partial=True; break
    direction=str(t.get("direction") or "")
    sign=1.0 if direction=="LONG" else -1.0
    issues=[]; strengths=[]; proposals=[]
    risk_atr=(abs(entry-stop)/atr) if entry and stop and atr else None
    target_atr=(abs(target-entry)/atr) if entry and target and atr else None
    rr=(abs(target-entry)/abs(entry-stop)) if entry and stop and target and abs(entry-stop)>0 else None
    anchor=_num(s.get("stop_anchor"))
    protected=s.get("protected_swing") or {}
    target_ladder=list(s.get("target_ladder") or [])
    structural_levels=s.get("structural_levels")
    stop_anchor_ok=None
    stop_anchor_buffer_atr=None
    if stop and anchor and atr:
        stop_anchor_ok=(stop<anchor if direction=="LONG" else stop>anchor)
        stop_anchor_buffer_atr=abs(stop-anchor)/atr
    first_zone=_num((target_ladder[0] or {}).get("price")) if target_ladder else None
    target_zone_match=(first_zone is not None and target is not None
                       and abs(first_zone-target)<=max(1e-9,abs(target)*1e-8))
    target_zone_distance_atr=(abs(target-entry)/atr if target and entry and atr else None)
    if net>0: strengths.append("Сделка закрыта с положительным результатом после расходов.")
    else: issues.append("Отрицательный финансовый результат после расходов.")
    cost_drag=bool(net<0 and gross>0 and fees+funding>=gross)
    episode_giveback=bool(net<0 and partial_seen)
    secondary_findings=[]
    if cost_drag:
        secondary_findings.append("COST_DRAG")
        issues.append("Положительный gross-результат был полностью съеден накопленными расходами.")
    if episode_giveback:
        secondary_findings.append("EPISODE_PROFIT_GIVEBACK")
        issues.append("В эпизоде уже была структурная фиксация прибыли, но итог сделки стал отрицательным.")
    if replan_after_partial and net<0:
        secondary_findings.append("TARGET_REPLAN_AFTER_PARTIAL")
        proposals.append(_proposal(t.get("trade_id"),"EPISODE_PROFIT_FLOOR",
            "Ограничить добор после уже заработанной прибыли итогом всей идеи на стопе",
            "После частичного TP новый подтверждённый ADD может перепланировать runner, "
            "но его размер не должен опускать прогнозируемый net-P&L всей идеи на активном стопе ниже нуля.",
            tests=[{"parameter":"episode_floor_rub","values":[0.0]},
                   {"parameter":"add_sizing","values":["CAP_TO_FLOOR","BLOCK_IF_NO_FULL_STEP"]}]))
    material_profit_threshold_pct=float(
        (((getattr(CTC,"TREND_ACCELERATION_POLICY",{}) or {}).get("profit_protection") or {})
         .get("mfe_activation_pct_points")) or 0.15
    )
    profit_protection_candidate=bool(
        net<=0 and mfe is not None and mfe>=material_profit_threshold_pct
    )
    if profit_protection_candidate:
        issues.append(
            "Сделка имела материальный ход в плюс, но закрылась в минус; "
            "нужен replay устойчивости прибыльных экскурсий и сопровождения."
        )
        proposals.append(_proposal(t.get("trade_id"),"PROFIT_MATURITY",
            "Проверить адаптивную защиту прибыли после устойчивой проторговки",
            "Защита не зависит от номера прибыльного захода. После материального MFE "
            "она допускается только если результат после расходов остаётся положительным "
            "достаточно долго и подтверждён несколькими независимыми окнами риск-таймфрейма.",
            tests=[{"parameter":"material_mfe_threshold_pct","values":[0.15]},
                   {"parameter":"minimum_positive_windows","values":[3,4]},
                   {"parameter":"maximum_required_dwell_minutes","values":[120,180,240]}]))
    if risk_atr is not None and risk_atr>4.0:
        issues.append("Начальный риск превышает 4 ATR выбранного риск-таймфрейма.")
        proposals.append(_proposal(t.get("trade_id"),"STOP_VOLATILITY",
            "Проверить слишком широкий структурный стоп",
            "Стоп должен оставаться за подтверждённым high/low, но экстремально широкий ATR-риск требует отдельного shadow-теста.",
            conflicts=["CTC38: размер подстраивается под структурный стоп; нельзя искусственно приблизить стоп ради размера."],
            tests=[{"parameter":"max_initial_risk_atr","values":[3.0,4.0,5.0]}]))
    if rr is not None and rr<1.0:
        issues.append("Исходная цель меньше исходного ценового риска.")
    if stop_anchor_ok is True:
        strengths.append("Стоп расположен за сохранённым предыдущим структурным high/low.")
    if stop_anchor_ok is False:
        issues.append("Стоп не подтверждён сохранённым предыдущим структурным high/low.")
    if target_ladder and target_zone_match:
        strengths.append("Первая цель совпадает с сохранённой ранее наблюдавшейся структурной зоной.")
    elif target and not target_ladder:
        issues.append("Для цели нет сохранённого доказательства предыдущей структурной зоны; проверить происхождение тейка.")
    if net>=0:
        primary_review_class="PROFITABLE_CONTROL"
    elif profit_protection_candidate or episode_giveback:
        primary_review_class="PROFIT_MANAGEMENT_REVIEW"
    else:
        primary_review_class="ENTRY_OR_DIRECTION_REVIEW"
    diag=t.get("trade_diagnostics") or {}
    eligible=bool(t.get("learning_eligible"))
    evidence="VERIFIED" if eligible else "UNVERIFIED"
    if not eligible:
        issues.append("Доказательная цепочка сделки неполна; выводы нельзя автоматически продвигать в канон.")
    return _copy({
        "version":VERSION,"trade_id":t.get("trade_id"),"episode_key":t.get("episode_key"),
        "status":"REVIEWED","evidence_status":evidence,
        "financial_result_rub":net,"gross_result_rub":gross,
        "costs_rub":fees+funding,"exit_reason":t.get("exit_reason"),
        "primary_review_class":primary_review_class,
        "secondary_findings":secondary_findings,
        "episode_lifecycle":{"partial_profit_seen":partial_seen,
            "target_replan_after_partial":replan_after_partial,
            "target_lifecycle_history":target_history[-16:]},
        "levels_volatility":{"entry":entry,"exit":exitp,
            "stop":initial_stop,"target":initial_target,
            "initial_stop":initial_stop,"initial_target":initial_target,
            "final_stop":final_stop,"final_target":final_target,
            "atr":atr,"initial_risk_atr":risk_atr,"target_distance_atr":target_atr,
            "gross_target_to_risk":rr,"stop_anchor":anchor,"stop_beyond_anchor":stop_anchor_ok,
            "stop_anchor_buffer_atr":stop_anchor_buffer_atr,
            "protected_swing":protected,"target_ladder":target_ladder,
            "target_matches_first_previous_zone":target_zone_match,
            "target_zone_distance_atr":target_zone_distance_atr,
            "structural_levels":structural_levels},
        "market_context":{"regime":s.get("regime"),"trigger_timeframe":s.get("trigger_timeframe"),
            "structural_timeframe":s.get("structural_timeframe"),
            "stop_timeframe":s.get("stop_timeframe"),"atr_timeframe":s.get("atr_timeframe"),
            "daily_ma":s.get("daily_ma"),"daily_ma_periods":s.get("daily_ma_periods"),
            "indicators":s.get("indicators"),"horizon_structure":s.get("horizon_structure")},
        "path":{"mfe_pct":mfe,"mae_pct":mae,"giveback_pct":t.get("giveback_pct"),
            "material_profit_threshold_pct":material_profit_threshold_pct,
            "profit_protection_candidate":profit_protection_candidate},
        "diagnostic_attribution":diag.get("primary_attribution"),
        "strengths":strengths,"issues":issues,"proposals":proposals,
        "governance":{"canonical_conflict_scan_required":bool(VOP.SELF_LEARNING.get("canonical_conflict_scan_required")),
            "parameter_changes":VOP.SELF_LEARNING.get("parameter_search_default"),"owner_approval_required":bool(VOP.SELF_LEARNING.get("owner_verification_required_for_rule_promotion")),
            "promotion_requirements":list(CTC.LEARNING_POLICY.get("promotion_requires") or ())}
    })
