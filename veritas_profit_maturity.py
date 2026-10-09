"""Owner-verified sustained-profit maturity and economic breakeven."""
from __future__ import annotations
import json, math
from datetime import datetime, timezone

import veritas_owner_policy as VOP
import veritas_costs as VC
import veritas_profit_protection as VPP
import veritas_timeframe_structure as TFS
from veritas_quote_time import utc_datetime

VERSION = "OWNER_SUSTAINED_PROFIT_BREAKEVEN_V1"

def _num(v):
    try:
        x=float(v)
        return x if math.isfinite(x) else None
    except (TypeError,ValueError):
        return None

def _payload(z):
    v=(z or {}).get("payload") or {}
    try:
        return json.loads(v) if isinstance(v,str) else dict(v)
    except (TypeError,ValueError):
        return {}

def _management_tf(z,p):
    entry_event=p.get("entry_event_snapshot") or {}
    entry_ctx=p.get("timeframe_entry_context") or entry_event.get("timeframe_entry_context") or {}
    event=entry_ctx.get("event") or entry_event
    return str(p.get("management_horizon") or p.get("r56_management_horizon")
               or event.get("structural_timeframe") or event.get("stop_timeframe")
               or p.get("structural_timeframe") or p.get("stop_timeframe")
               or p.get("execution_horizon") or p.get("entry_horizon")
               or (z or {}).get("horizon") or "")

def _merge_position(z, patch, new_stop=None):
    out=dict(z or {})
    p=_payload(out); p.update(patch or {}); out["payload"]=p
    if new_stop is not None:
        out["stop_price"]=new_stop
    return out

def observe(c, name, position, quote, now=None, commission=VC.COMMISSION_RATE):
    """Arm economic breakeven only after an adaptively sustained profitable state.

    The owner rule is duration/stability based, not excursion-count based. A brief
    favorable spike never moves the synthetic breakeven. The episode starts after
    material MFE is observed, remains alive while projected whole-trade economics
    stay positive after costs, and must persist across independent management
    windows for an adaptive dwell derived from the risk timeframe.
    """
    z=dict(position or {}); p=_payload(z); q=dict(quote or {})
    out={"version":VERSION,"eligible":False,"mature":bool(p.get("profit_maturity_armed")),
         "reason":"PROFIT_MATURITY_CONTEXT_REQUIRED","patch":{},"new_stop":None}
    direction=str(z.get("direction") or "")
    entry=_num(z.get("avg_entry_price")); price=_num(q.get("price"))
    observed=TFS.timestamp(q.get("observed_at")); opened=TFS.timestamp(z.get("opened_at"))
    clock=utc_datetime(now) or datetime.now(timezone.utc)
    tf=_management_tf(z,p)
    if direction not in ("LONG","SHORT") or not entry or not price or observed is None or opened is None:
        return out
    if observed < opened or observed > clock.timestamp()+1:
        return dict(out,reason="PROFIT_MATURITY_TIME_INVALID")

    sign=1.0 if direction=="LONG" else -1.0
    favorable_pct=100.0*sign*(price/entry-1.0)
    recorded_mfe=max(0.0,_num(z.get("mfe_pct")) or 0.0,_num(p.get("mfe_pct")) or 0.0)
    observed_mfe=max(recorded_mfe,favorable_pct)

    try:
        mark_assessment=VPP.assess(c,z,stop=price,price=price,now=clock,commission=commission)
        net_now=_num((mark_assessment.get("net_profit_protection") or {}).get("net_at_stop_rub"))
    except Exception:
        mark_assessment={}
        net_now=None

    policy=VOP.PROFIT_MATURITY
    threshold=float(policy.get("material_mfe_threshold_pct") or 0.15)
    min_windows=int(policy.get("minimum_positive_windows") or 3)
    tf_seconds=int(TFS.TIMEFRAMES.get(tf) or 60)
    window_seconds=max(int(policy.get("window_seconds_floor") or 300),
                       min(tf_seconds,int(policy.get("window_seconds_ceiling") or 3600)))
    required_dwell=max(int(policy.get("minimum_dwell_seconds_floor") or 600),
                       min(int(policy.get("maximum_required_dwell_seconds") or 10800),
                           min_windows*window_seconds))
    positive_after_costs=bool(net_now is not None and net_now>0)
    material=bool(observed_mfe>=threshold)

    active=bool(p.get("profit_maturity_stable_episode_active"))
    started=_num(p.get("profit_maturity_stable_episode_started_at"))
    last_window=p.get("profit_maturity_last_window")
    positive_windows=int(p.get("profit_maturity_positive_windows") or 0)
    changed=False

    # A stable-profit episode may begin only after material favorable movement is
    # observed. Once begun it can breathe below the MFE threshold, but it ends if
    # projected whole-trade net economics are no longer positive after costs.
    if active and not positive_after_costs:
        active=False; started=None; last_window=None; positive_windows=0; changed=True
    if (not active) and material and positive_after_costs:
        active=True; started=observed; last_window=None; positive_windows=0; changed=True

    bucket=int(observed//window_seconds)
    if active and positive_after_costs and (last_window is None or int(last_window)!=bucket):
        positive_windows+=1; last_window=bucket; changed=True

    dwell=max(0.0,observed-started) if active and started is not None else 0.0
    armed=bool(p.get("profit_maturity_armed"))
    if (not armed and active and material and positive_after_costs
            and positive_windows>=min_windows and dwell>=required_dwell):
        armed=True; changed=True

    patch={
        "profit_maturity_version":VERSION,
        "profit_maturity_activation_mode":policy.get("activation_mode"),
        "profit_maturity_management_timeframe":tf or None,
        "profit_maturity_material_mfe_threshold_pct":threshold,
        "profit_maturity_observed_mfe_pct":observed_mfe,
        "profit_maturity_last_favorable_pct":favorable_pct,
        "profit_maturity_last_projected_net_rub":net_now,
        "profit_maturity_window_seconds":window_seconds,
        "profit_maturity_required_positive_windows":min_windows,
        "profit_maturity_required_dwell_seconds":required_dwell,
        "profit_maturity_stable_episode_active":active,
        "profit_maturity_stable_episode_started_at":started,
        "profit_maturity_last_window":last_window,
        "profit_maturity_positive_windows":positive_windows,
        "profit_maturity_dwell_seconds":dwell,
        "profit_maturity_armed":armed,
        "profit_maturity_armed_at":(p.get("profit_maturity_armed_at") or
                                    (clock.isoformat() if armed else None)),
        "profit_maturity_owner_teaching_id":VOP.TEACHING_ID,
    }

    new_stop=None
    floor_satisfied=bool(p.get("profit_maturity_floor_satisfied"))
    if armed:
        try:
            base=VPP.assess(c,z,price=price,now=clock,commission=commission)
            be=_num((base.get("net_profit_protection") or {}).get("break_even_stop_price"))
        except Exception:
            be=None
        old=VPP.effective_stop(z)
        if be and old:
            candidate=max(old,be) if direction=="LONG" else min(old,be)
            already_protected=sign*(old-be)>=-1e-12
            improves=sign*(candidate-old)>1e-12
            remains_live=sign*(price-candidate)>0
            floor_satisfied=bool(already_protected or (improves and remains_live))
            patch.update({"profit_maturity_break_even_reference":be,
                          "profit_maturity_floor_satisfied":floor_satisfied})
            if improves and remains_live:
                try:
                    protection=VPP.assess(c,z,stop=candidate,price=price,now=clock,commission=commission)
                except Exception:
                    protection={}
                patch.update(protection)
                patch.update({"trailing_stop":candidate,"trailing_rule":VERSION,
                              "trailing_stage":"ECONOMIC_BREAKEVEN_MATURE",
                              "profit_maturity_break_even_stop":candidate})
                new_stop=candidate; changed=True
        else:
            patch["profit_maturity_floor_satisfied"]=False

    if changed:
        encoded=json.dumps(patch,ensure_ascii=False,allow_nan=False,default=str)
        if new_stop is None:
            c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s",
                      (encoded,name,z.get("asset"),z.get("active_trade_id")))
        else:
            c.execute("UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s AND stop_price IS NOT DISTINCT FROM %s",
                      (new_stop,encoded,name,z.get("asset"),z.get("active_trade_id"),z.get("stop_price")))
        if z.get("active_trade_id"):
            c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                      (encoded,z.get("active_trade_id")))
    return {"version":VERSION,"eligible":True,"mature":armed,
            "reason":"PROFIT_MATURITY_ARMED" if armed else "PROFIT_MATURITY_OBSERVING",
            "material":material,"material_mfe_threshold_pct":threshold,
            "stable_episode_active":active,"positive_after_costs":positive_after_costs,
            "positive_windows":positive_windows,"required_windows":min_windows,
            "dwell_seconds":dwell,"required_dwell_seconds":required_dwell,
            "projected_net_rub":net_now,"patch":patch,"new_stop":new_stop,
            "position":_merge_position(z,patch,new_stop)}
