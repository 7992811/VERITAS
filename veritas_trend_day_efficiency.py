"""Trend-day efficiency overlay for owner-approved paper acceleration.

This module never creates direction, fetches data, or writes state. It grades
an already directional setup using evidence already present on the decision row.
Cross-asset context is recorded only; v1 gives it zero sizing influence.
"""
from __future__ import annotations
import math

import veritas_canonical_constitution as CTC


def _num(*values):
    for value in values:
        if value is None:
            continue
        try:
            x=float(value)
            if math.isfinite(x):
                return x
        except Exception:
            pass
    return None


def assess(row,direction,policy,*,mid=False,senior=False,evidence=None,
           expected=None,progress=None):
    cfg=getattr(CTC,'TREND_DAY_EFFICIENCY_POLICY',{}) or {}
    row=row or {}; policy=policy or {}
    mode=str(policy.get('mode') or '')
    out={'eligible':False,'score':0.0,'coverage':0.0,
         'reason':'TREND_DAY_NOT_CONFIRMED'}
    if not cfg.get('enabled') or mode=='CURRENCY' or direction not in ('LONG','SHORT'):
        return out

    plan=row.get('trade_plan') or {}
    ti=row.get('trend_impulse') or {}
    st=row.get('intraday_structure') or {}
    hs=row.get('horizon_structure') or {}
    phase=str(ti.get('phase') or row.get('trend_phase') or '')
    if phase not in set(cfg.get('accepted_phases') or ('TREND_DAY','IMPULSE_TREND')):
        return dict(out,reason='TREND_DAY_PHASE_REQUIRED',phase=phase)

    observed_direction=str(ti.get('direction') or st.get('direction') or direction)
    if observed_direction in ('LONG','SHORT') and observed_direction!=direction:
        return dict(out,reason='TREND_DAY_DIRECTION_CONFLICT',phase=phase)
    entryq=str(ti.get('entry_quality') or st.get('entry_quality')
               or plan.get('entry_quality') or '')
    if entryq in ('INVALIDATED','LATE_EXTENDED','EXTENDED_WAIT_PULLBACK'):
        return dict(out,reason='TREND_DAY_ENTRY_QUALITY_INVALID',
                    phase=phase,entry_quality=entryq)

    inst=row.get('institutional_signal') or {}
    if evidence is None:
        try:
            evidence=int(row.get('independent_evidence_families')
                         or ((inst.get('evidence_independence') or {}).get('independent_count'))
                         or 0)
        except Exception:
            evidence=0
    if expected is None:
        expected=abs(_num(plan.get('expected_move_pct'),
                          row.get('expected_move_pct'),0.0) or 0.0)
    if progress is None:
        context=row.get('trend_entry_context') or plan.get('trend_entry_context') or {}
        progress=_num((context.get('event') or {}).get('target_progress'),0.0) or 0.0

    if int(evidence or 0)<int(cfg.get('minimum_independent_evidence') or 4):
        return dict(out,reason='TREND_DAY_EVIDENCE_INSUFFICIENT',
                    phase=phase,evidence=evidence)
    if float(expected or 0.0)<float(cfg.get('minimum_expected_move_pct') or .006):
        return dict(out,reason='TREND_DAY_REMAINING_MOVE_TOO_SMALL',
                    phase=phase,evidence=evidence,expected_move_pct=expected)
    if float(progress or 0.0)>float(cfg.get('maximum_target_progress') or .50):
        return dict(out,reason='TREND_DAY_TARGET_MOSTLY_SPENT',
                    phase=phase,evidence=evidence,target_progress=progress)
    if cfg.get('require_mid_confirmation') and not mid:
        return dict(out,reason='TREND_DAY_MID_CONFIRMATION_REQUIRED',
                    phase=phase,evidence=evidence)
    if cfg.get('require_senior_confirmation') and not senior:
        return dict(out,reason='TREND_DAY_SENIOR_CONFIRMATION_REQUIRED',
                    phase=phase,evidence=evidence)

    components=[]
    def add(name,weight,value,available=True):
        if not available or value is None:
            components.append({'name':name,'weight':weight,
                               'available':False,'value':None})
            return
        components.append({'name':name,'weight':weight,'available':True,
                           'value':max(0.0,min(1.0,float(value)))})

    add('phase',.18,1.0 if phase=='IMPULSE_TREND' else .85)
    impulse=_num(ti.get('impulse_score'),row.get('impulse_score'))
    add('impulse',.16,impulse,impulse is not None)
    structure=_num(st.get('score'),ti.get('structure_score'),hs.get('score'),
                   row.get('horizon_structure_score'))
    add('structure',.14,structure,structure is not None)
    efficiency=_num(ti.get('session_efficiency'),st.get('session_efficiency'),
                    row.get('session_efficiency'))
    add('session_efficiency',.10,efficiency,efficiency is not None)
    persistence=_num(ti.get('session_persistence'),st.get('session_persistence'),
                     row.get('session_persistence'))
    add('session_persistence',.10,persistence,persistence is not None)
    consensus=_num(ti.get('horizon_consensus_score'))
    if consensus is None:
        count=_num(ti.get('horizon_consensus_count'))
        consensus=min(1.0,count/4.0) if count is not None else None
    add('horizon_consensus',.10,consensus,consensus is not None)
    add('independent_evidence',.08,min(1.0,float(evidence or 0)/5.0))
    add('mid_confirmation',.06,1.0 if mid else 0.0)
    add('senior_confirmation',.06,1.0 if senior else 0.0)
    relvol=_num(st.get('relative_volume'),row.get('relative_volume'))
    add('relative_volume',.02,
        min(1.0,max(0.0,relvol)/1.5) if relvol is not None else None,
        relvol is not None and relvol>0)

    available_weight=sum(x['weight'] for x in components if x['available'])
    weighted=sum(x['weight']*x['value'] for x in components if x['available'])
    score=weighted/available_weight if available_weight>0 else 0.0
    coverage=available_weight

    cross=row.get('cross_asset_shadow') or plan.get('cross_asset_shadow') or {}
    cross_regime=str(cross.get('regime') or '')
    cross_alignment=('ALIGNED' if ((direction=='LONG' and cross_regime=='RISK_ON')
                                   or (direction=='SHORT' and cross_regime=='RISK_OFF'))
                     else 'CONFLICT' if cross_regime in ('RISK_ON','RISK_OFF')
                     else 'UNAVAILABLE')
    eligible=bool(score>=float(cfg.get('minimum_score') or .78)
                  and coverage>=float(cfg.get('minimum_coverage') or .75))
    runner_cfg=cfg.get('runner') or {}
    runner=(float(runner_cfg.get('impulse_trend_ratio') or .85)
            if phase=='IMPULSE_TREND'
            else float(runner_cfg.get('trend_day_ratio') or .75))
    runner=min(float(runner_cfg.get('maximum_ratio') or .85),max(.50,runner))
    return {
        'eligible':eligible,
        'reason':'TREND_DAY_EXTREME_CONFIRMED' if eligible else 'TREND_DAY_SCORE_BELOW_FLOOR',
        'score':score,'coverage':coverage,'phase':phase,'entry_quality':entryq,
        'evidence':int(evidence or 0),'expected_move_pct':float(expected or 0.0),
        'target_progress':float(progress or 0.0),
        'mid_confirmation':bool(mid),'senior_confirmation':bool(senior),
        'components':components,'runner_ratio':runner,
        'cross_asset_regime':cross_regime or None,
        'cross_asset_alignment':cross_alignment,
        'cross_asset_size_influence':False,
        'policy_version':cfg.get('version'),
    }


def protected_runner_ratio(position,payload):
    """Return a larger TP1 runner only after positive-net protection is active."""
    p=payload or {}
    if str((position or {}).get('portfolio_name') or '')=='Currency':
        return .50
    cfg=(getattr(CTC,'TREND_DAY_EFFICIENCY_POLICY',{}) or {}).get('runner') or {}
    protected=bool(p.get('r_accel_mfe_profit_lock_active')
                   or p.get('r55_net_profit_lock_active'))
    if cfg.get('requires_profit_protection',True) and not protected:
        return .50

    td=p.get('last_trend_day_efficiency') or {}
    if isinstance(td,dict) and td.get('eligible'):
        try:
            ratio=float(td.get('runner_ratio') or .50)
        except Exception:
            ratio=.50
        return min(float(cfg.get('maximum_ratio') or .85),max(.50,ratio))

    if p.get('r46_trend_hold_active'):
        phase=str(p.get('r46_trend_phase') or '')
        if phase in ('TREND_DAY','IMPULSE_TREND'):
            try:
                ratio=float(p.get('r46_tp_runner_ratio') or .50)
            except Exception:
                ratio=.50
            return min(float(cfg.get('maximum_ratio') or .85),max(.50,ratio))
    return .50
