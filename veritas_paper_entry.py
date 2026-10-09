"""Final paper entry economics, size and fill without I/O or strategy overrides."""
import math

import veritas_canonical_constitution as CTC
import veritas_execution as VX
import veritas_execution_snapshot as VES
import veritas_price_source as VPS
import veritas_stop_risk as VSR
from veritas_quote_time import utc_datetime


def _failure(reason, gate=None, budget=None, status='BLOCKED'):
    gate=gate or {}
    blockers=list(gate.get('blockers') or [reason])
    if reason!='FINAL_EXECUTION_ECONOMICS' and reason not in blockers:
        blockers.append(reason)
    return {'eligible':False,'status':status,'reason':reason,'gate':gate,
            'stop_risk_budget':budget,'blockers':blockers,
            'hard_blockers':[code for code in blockers if CTC.veto_severity(code)=='HARD']}


def prepare(row, quote, direction, requested, position, nav, ts, policy, *, gross_excluding_position=0.0):
    """Any size change gets a new check on the same detached quote before recording."""
    row=VPS.execution_row(dict(row,_execution_quote=quote))
    asset=row.get('asset')
    price=VPS.positive(quote.get('price'))
    clock=utc_datetime(ts)
    if not price or not math.isfinite(nav) or nav<=0 or clock is None:
        return _failure('ENTRY_EXECUTION_INPUT_INVALID')
    current=abs(float(position.get('units') or 0.0))*price/nav if position else 0.0
    if requested<=current+0.0025:
        return _failure('TARGET_ALREADY_REACHED',status='HELD')
    canonical=bool((row.get('_canonical_admission') or {}).get('open'))
    policy=dict(policy)
    # A confirmed trend-acceleration state may temporarily earn more nominal
    # exposure, but never more stop-risk. Currency/live semantics are excluded.
    acceleration=row.get('_trend_acceleration') or {}
    if acceleration.get('active') and str(policy.get('mode') or '')!='CURRENCY':
        cfg=getattr(CTC,'TREND_ACCELERATION_POLICY',{}) or {}
        caps=(cfg.get('temporary_caps') or {}).get(str(policy.get('mode') or '')) or {}
        if caps:
            base_fraction=float(policy.get('max_fraction',policy.get('max_single_asset_fraction',0.0)) or 0.0)
            base_gross=float(policy.get('max_gross') or 0.0)
            policy['max_fraction']=max(base_fraction,float(caps.get('max_fraction') or base_fraction))
            policy['max_single_asset_fraction']=policy['max_fraction']
            policy['max_gross']=max(base_gross,float(caps.get('max_gross') or base_gross))
    governor=(row.get('_canonical_admission') or {}).get('risk_governor') or {}
    if governor.get('new_risk') is False:
        return _failure('RISK_GOVERNOR_HARD_STOP')
    if governor.get('max_gross') is not None:
        policy['max_gross']=min(float(policy['max_gross']),float(governor['max_gross']))
    def evaluate(total):
        gate=VX.entry_gate(row,price,direction,total,position,
            existing_target_price=VX.stored_position_target_price(position),now=clock,
            execution_fraction=max(0.0,total-current))
        blockers=list(dict.fromkeys(gate.get('blockers') or []))
        if not gate.get('eligible') and canonical and blockers and all(
                CTC.veto_severity(code)=='SOFT' for code in blockers):
            gate=dict(gate,eligible=True,status='PASS_CTC_V2_ACCOUNTING',
                      canonical_soft_override=True,canonical_overridden_blockers=blockers,
                      canonical_hard_blockers=[])
        return gate
    gate=evaluate(requested)
    if not gate.get('eligible'):
        return _failure('FINAL_EXECUTION_ECONOMICS',gate)
    def size(total, checked):
        reserve={'eligible':True,'net_stop_risk_nav':0.0}
        if position:
            hold=checked.get('expected_hold_seconds')
            reserve=VSR.existing_stop_risk_nav(position,checked.get('modeled_stop_fill'),
                nav,clock,mark_price=price,expected_hold_seconds=hold)
        if not reserve.get('eligible'):
            return reserve
        budget=VSR.cap_fraction_from_economics(checked,total,policy,
            current_fraction=current,existing_stop_risk_nav=reserve['net_stop_risk_nav'],
            gross_excluding_position=gross_excluding_position)
        budget['existing_position_risk']=reserve
        return budget
    budget=size(requested,gate)
    if not budget.get('eligible'):
        return _failure(budget.get('reason'),gate,budget,
                        status='HELD' if budget.get('status')=='HOLD' else 'BLOCKED')
    total=budget['fraction']
    if not math.isclose(total,requested,rel_tol=0,abs_tol=1e-12):
        gate=evaluate(total)
        if not gate.get('eligible'):
            return _failure('FINAL_EXECUTION_ECONOMICS',gate,budget)
        revised=size(total,gate)
        if not revised.get('eligible') or not math.isclose(revised.get('fraction',0),total,abs_tol=1e-12):
            return _failure('ENTRY_SIZE_RECHECK_REQUIRED',gate,revised)
        revised['requested_fraction']=requested
        budget=revised
    fill=VES.checked_fill(gate,quote,asset,direction,price,total-current)
    if fill is None:
        return _failure('EXECUTION_SNAPSHOT_MISMATCH',gate,budget)
    return {'eligible':True,'status':'PASS','reason':'VERIFIED_EXECUTION_READY',
            'gate':gate,'stop_risk_budget':budget,'target_fraction':total,
            'add_notional_rub':(total-current)*nav,'fill':fill,
            'trend_acceleration':acceleration if acceleration.get('active') else None}
