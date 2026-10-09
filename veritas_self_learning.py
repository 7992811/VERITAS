"""Closed-trade postmortem used by the Self-learning UI.

The module is diagnostic and proposal-only. It never mutates canonical policy.
"""
from __future__ import annotations
import hashlib, json, math
import veritas_canonical_constitution as CTC

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
    stop=_num(t.get("stop_price")); target=_num(t.get("take_price"))
    atr=_num(s.get("atr")); mfe=_num(t.get("mfe_pct")); mae=_num(t.get("mae_pct"))
    net=_num(t.get("net_pnl_rub")) or 0.0
    direction=str(t.get("direction") or "")
    sign=1.0 if direction=="LONG" else -1.0
    issues=[]; strengths=[]; proposals=[]
    risk_atr=(abs(entry-stop)/atr) if entry and stop and atr else None
    target_atr=(abs(target-entry)/atr) if entry and target and atr else None
    rr=(abs(target-entry)/abs(entry-stop)) if entry and stop and target and abs(entry-stop)>0 else None
    anchor=_num(s.get("stop_anchor"))
    stop_anchor_ok=None
    if stop and anchor and atr:
        stop_anchor_ok=(stop<anchor if direction=="LONG" else stop>anchor)
    if net>0: strengths.append("Сделка закрыта с положительным результатом после расходов.")
    else: issues.append("Отрицательный финансовый результат после расходов.")
    if mfe is not None and mfe>0 and net<=0:
        issues.append("Сделка была в плюсе, но прибыль не была сохранена.")
        proposals.append(_proposal(t.get("trade_id"),"PROFIT_MATURITY",
            "Проверить защиту прибыли после устойчивой проторговки",
            "Первые два прибыльных окна не меняют исходный стоп; после третьего подряд и минимальной выдержки тестировать истинный безубыток после расходов.",
            tests=[{"parameter":"positive_windows","values":[3,4]},
                   {"parameter":"minimum_dwell_minutes","values":[10,15,20]}]))
    if risk_atr is not None and risk_atr>4.0:
        issues.append("Начальный риск превышает 4 ATR выбранного риск-таймфрейма.")
        proposals.append(_proposal(t.get("trade_id"),"STOP_VOLATILITY",
            "Проверить слишком широкий структурный стоп",
            "Стоп должен оставаться за подтверждённым high/low, но экстремально широкий ATR-риск требует отдельного shadow-теста.",
            conflicts=["CTC38: размер подстраивается под структурный стоп; нельзя искусственно приблизить стоп ради размера."],
            tests=[{"parameter":"max_initial_risk_atr","values":[3.0,4.0,5.0]}]))
    if rr is not None and rr<1.0:
        issues.append("Исходная цель меньше исходного ценового риска.")
    if stop_anchor_ok is True: strengths.append("Стоп расположен за сохранённым структурным high/low.")
    if stop_anchor_ok is False: issues.append("Стоп не подтверждён сохранённым структурным high/low.")
    diag=t.get("trade_diagnostics") or {}
    eligible=bool(t.get("learning_eligible"))
    evidence="VERIFIED" if eligible else "UNVERIFIED"
    if not eligible:
        issues.append("Доказательная цепочка сделки неполна; выводы нельзя автоматически продвигать в канон.")
    return _copy({
        "version":VERSION,"trade_id":t.get("trade_id"),"episode_key":t.get("episode_key"),
        "status":"REVIEWED","evidence_status":evidence,
        "financial_result_rub":net,"exit_reason":t.get("exit_reason"),
        "levels_volatility":{"entry":entry,"exit":exitp,"stop":stop,"target":target,
            "atr":atr,"initial_risk_atr":risk_atr,"target_distance_atr":target_atr,
            "gross_target_to_risk":rr,"stop_anchor":anchor,"stop_beyond_anchor":stop_anchor_ok},
        "market_context":{"regime":s.get("regime"),"trigger_timeframe":s.get("trigger_timeframe"),
            "structural_timeframe":s.get("structural_timeframe"),
            "stop_timeframe":s.get("stop_timeframe"),"atr_timeframe":s.get("atr_timeframe"),
            "daily_ma":s.get("daily_ma"),"daily_ma_periods":s.get("daily_ma_periods"),
            "indicators":s.get("indicators"),"horizon_structure":s.get("horizon_structure")},
        "path":{"mfe_pct":mfe,"mae_pct":mae,"giveback_pct":t.get("giveback_pct")},
        "diagnostic_attribution":diag.get("primary_attribution"),
        "strengths":strengths,"issues":issues,"proposals":proposals,
        "governance":{"canonical_conflict_scan_required":True,
            "parameter_changes":"SHADOW_ONLY","owner_approval_required":True,
            "promotion_requirements":list(CTC.LEARNING_POLICY.get("promotion_requires") or ())}
    })
