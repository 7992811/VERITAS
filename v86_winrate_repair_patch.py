from pathlib import Path
import sys,re as _re

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# 1) Tighten tactical reversal itself: this is a fast setup, not a generic directional substitute.
p=root/'veritas_intelligence.py'
s=p.read_text(encoding='utf-8')
old="    active=bool(count>=4 and prob>=0.70 and rr>=1.30)\n"
new="    active=bool(count>=5 and prob>=0.76 and rr>=1.50)\n"
if old not in s: raise SystemExit('WINRATE_TACTICAL_THRESHOLD_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="                if tactical_reversal.get('active') and research_dec==tactical_reversal.get('direction'):\n"
new="                if tactical_reversal.get('active') and research_dec==tactical_reversal.get('direction') and horizon in ('1h','4h'):\n"
if old not in s: raise SystemExit('WINRATE_TACTICAL_HORIZON_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# 2) Cost-adjusted edge and noise-aware stop gate.
old="    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0\n"
new="""    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0
    # User portfolio accounting charges 5bp per side. Add a small paper slippage reserve.
    round_trip_fee=0.0010
    slippage_reserve=0.0002
    all_in_cost=round_trip_fee+slippage_reserve
    horizon_scale={'1h':1.0,'4h':4.0,'1d':8.0,'3d':24.0,'7d':40.0}.get(horizon,1.0)
    absolute_noise_floor={'1h':0.0015,'4h':0.0025,'1d':0.0040,'3d':0.0060,'7d':0.0080}.get(horizon,0.0015)
    noise_floor=max(absolute_noise_floor,0.85*sigma*math.sqrt(horizon_scale))
    net_reward=max(0.0,exp-all_in_cost)
    net_risk=stop_dist+all_in_cost
    net_ratio=net_reward/max(net_risk,1e-12)
"""
if old not in s: raise SystemExit('WINRATE_RR_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""    risk_reward_ok=bool(ratio>=TRADE_MIN_EXPECTED_TO_STOP)
    eligible=bool(not invalid and p>0 and risk_reward_ok)
    return {'eligible':eligible,'reason':'ok' if eligible else ('invalidated' if invalid else 'expected_move_too_small_vs_stop'),
"""
new="""    risk_reward_ok=bool(ratio>=TRADE_MIN_EXPECTED_TO_STOP)
    noise_ok=bool(stop_dist>=noise_floor)
    net_edge_ok=bool(net_ratio>=1.25)
    eligible=bool(not invalid and p>0 and risk_reward_ok and noise_ok and net_edge_ok)
    reason=('ok' if eligible else
            'invalidated' if invalid else
            'stop_inside_expected_noise' if not noise_ok else
            'net_edge_after_cost_too_small' if not net_edge_ok else
            'expected_move_too_small_vs_stop')
    return {'eligible':eligible,'reason':reason,
"""
if old not in s: raise SystemExit('WINRATE_ELIGIBLE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""            'stop_distance_pct':stop_dist,'invalidation_price':inv,'expected_move_pct':exp,
            'expected_move_method':est.get('method'),'expected_to_stop_ratio':ratio,'min_expected_to_stop_ratio':TRADE_MIN_EXPECTED_TO_STOP,
"""
new="""            'stop_distance_pct':stop_dist,'invalidation_price':inv,'expected_move_pct':exp,
            'expected_move_method':est.get('method'),'expected_to_stop_ratio':ratio,'min_expected_to_stop_ratio':TRADE_MIN_EXPECTED_TO_STOP,
            'all_in_cost_fraction':all_in_cost,'noise_floor_stop_distance_pct':noise_floor,
            'net_expected_move_pct':net_reward,'net_expected_to_stop_ratio':net_ratio,
            'winrate_repair_mode':'HISTORICAL_EDGE_FAIL_GUARD',
"""
if old not in s: raise SystemExit('WINRATE_RETURN_FIELDS_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Make uncalibrated numbers visibly non-probabilistic in the product UI.
old="function probText(p,src){return p==null?'—':(100*Number(p)).toFixed(1)+'%'+(src==='EMPIRICAL_CALIBRATION'?' калибр.':' модельн.')}"
new="function probText(p,src){if(src!=='EMPIRICAL_CALIBRATION')return 'BUILDING';return p==null?'—':(100*Number(p)).toFixed(1)+'% калибр.'}"
if old in s:
    s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 3) Final admission gate: no more routing an ineligible plan just because it is directional.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
insert="""def winrate_repair_gate(row,direction):
    pp=row.get('trade_plan') or {}
    ti=pp.get('trade_integrity') or row.get('trade_integrity') or {}
    arb=pp.get('rule_arbitration') or row.get('rule_arbitration') or {}
    h=str(row.get('horizon') or '')
    setup=str(pp.get('setup') or '')
    tr=row.get('tactical_reversal') or {}
    if not setup and tr.get('active') and tr.get('direction')==direction:
        setup=str(tr.get('setup') or 'TACTICAL_REVERSAL')
    if ti.get('hard_invalidation') or arb.get('hard_veto'):
        return {'eligible':False,'reason':'HARD_INVALIDATION','max_fraction':D('0')}
    if pp.get('eligible') is False:
        return {'eligible':False,'reason':'TRADE_PLAN_NOT_ELIGIBLE:'+str(pp.get('reason') or 'unknown'),'max_fraction':D('0')}
    if ti.get('entry_permission')=='WAIT_ENTRY':
        return {'eligible':False,'reason':'ENTRY_PERMISSION_WAIT','max_fraction':D('0')}
    if setup=='TACTICAL_REVERSAL' and h not in ('1h','4h'):
        return {'eligible':False,'reason':'TACTICAL_REVERSAL_WRONG_HORIZON','max_fraction':D('0')}
    price=fnum(row.get('price'),0.0); stop=fnum(pp.get('stop_price'),0.0)
    if price<=0 or stop<=0 or not ((direction=='LONG' and stop<price) or (direction=='SHORT' and stop>price)):
        return {'eligible':False,'reason':'INVALID_STOP_GEOMETRY','max_fraction':D('0')}
    stop_dist=abs(price-stop)/price
    noise_floor=fnum(pp.get('noise_floor_stop_distance_pct'),0.0)
    if noise_floor>0 and stop_dist+1e-12<noise_floor:
        return {'eligible':False,'reason':'STOP_INSIDE_EXPECTED_NOISE','stop_distance_pct':stop_dist,
                'noise_floor':noise_floor,'max_fraction':D('0')}
    net_rr=fnum(pp.get('net_expected_to_stop_ratio'),0.0)
    if net_rr<1.25:
        return {'eligible':False,'reason':'NET_EDGE_AFTER_COST_FAIL','net_rr':net_rr,'max_fraction':D('0')}
    prob,src=entry_probability(row)
    inst=row.get('institutional_signal') or {}
    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    if src!='EMPIRICAL_CALIBRATION':
        if prob<.72:
            return {'eligible':False,'reason':'UNCALIBRATED_SCORE_TOO_WEAK','score':prob,'source':src,'max_fraction':D('0')}
        if indep<2:
            return {'eligible':False,'reason':'INSUFFICIENT_INDEPENDENT_EVIDENCE','independent':indep,'source':src,'max_fraction':D('0')}
    # Historical OOS/VAULT gate is currently FAIL. Keep new risk to one 5% research probe
    # until a new independent validation explicitly changes this policy.
    return {'eligible':True,'reason':'WINRATE_REPAIR_PROBE','score':prob,'source':src,'independent':indep,
            'net_rr':net_rr,'max_fraction':D('.05'),'historical_edge_gate':'FAIL'}

"""
anchor="def entry_probability(row):"
idx=s.find(anchor)
if idx<0: raise SystemExit('WINRATE_ENTRY_PROB_FUNCTION_NOT_FOUND')
# Insert after full entry_probability function by finding the next double-newline + def.
m=_re.search(r"def entry_probability\(row\):\n.*?\n\n(?=def |class |@)",s[idx:],flags=_re.S)
if not m: raise SystemExit('WINRATE_ENTRY_PROB_BLOCK_NOT_FOUND')
end=idx+m.end()
if 'def winrate_repair_gate(row,direction):' not in s:
    s=s[:end]+insert+s[end:]

pat=r"""        executable=\[\]; probeable=\[\]\n        for rr,ww in ranked:\n.*?            traces.append\(\{'asset':a,'direction':d,'horizon':h,'status':'ROUTED_CONFIRMED',\n                           'target_fraction':str\(desired\),'probability':round\(prob,4\),\n                           'probability_source':prob_source\}\)\n"""
m=_re.search(pat,s,flags=_re.S)
if not m: raise SystemExit('WINRATE_ROUTING_BRIDGE_BLOCK_NOT_FOUND')
replacement="""        executable=[]; rejected=[]
        for rr,ww in ranked:
            gate=winrate_repair_gate(rr,d)
            if not gate.get('eligible'):
                rejected.append({'horizon':rr.get('horizon'),'reason':gate.get('reason')})
                continue
            pp=dict(rr.get('trade_plan') or {})
            pp['winrate_repair_gate']=gate
            executable.append((rr,ww,pp,gate))
        if not executable:
            traces.append({'asset':a,'direction':d,'status':'BLOCKED','reason':'WINRATE_REPAIR_NO_QUALIFIED_HORIZON',
                           'candidate_horizons':[x['horizon'] for x,_ in ranked],
                           'rejections':rejected[:5]})
            continue
        r,w,p,gate=executable[0]
        h=r['horizon']; fam=family(r)
        prob,prob_source=entry_probability(r)
        # Repair mode is deliberately conservative: 5% research probe only.
        desired=min(D('.05'),gate.get('max_fraction') or D('.05'))
        traces.append({'asset':a,'direction':d,'horizon':h,'status':'ROUTED_REPAIR_PROBE',
                       'reason':gate.get('reason'),'target_fraction':str(desired),
                       'probability':None if prob_source!='EMPIRICAL_CALIBRATION' else round(prob,4),
                       'probability_source':prob_source,
                       'net_rr':gate.get('net_rr'),'independent':gate.get('independent'),
                       'historical_edge_gate':gate.get('historical_edge_gate')})
"""
s=s[:m.start()]+replacement+s[m.end():]
p.write_text(s,encoding='utf-8')

# 4) Portfolio specialization: Impulse stops duplicating reversal trades.
p=root/'veritas_v86/portfolios.py'
s=p.read_text(encoding='utf-8')
s=s.replace('("IMPULSE_GENESIS","IMPULSE_PIVOT_BREAK","TACTICAL_REVERSAL","RANGE_RETEST_BREAKOUT")',
            '("IMPULSE_GENESIS","IMPULSE_PIVOT_BREAK","RANGE_RETEST_BREAKOUT")')
p.write_text(s,encoding='utf-8')

p=root/'veritas_v86/application.py'
s=p.read_text(encoding='utf-8')
s=s.replace("if account=='Impulse' and s and s.setup_family not in ('IMPULSE_GENESIS','IMPULSE_PIVOT_BREAK','TACTICAL_REVERSAL','RANGE_RETEST_BREAKOUT'):",
            "if account=='Impulse' and s and s.setup_family not in ('IMPULSE_GENESIS','IMPULSE_PIVOT_BREAK','RANGE_RETEST_BREAKOUT'):")
p.write_text(s,encoding='utf-8')

print('V86_WINRATE_REPAIR_ACTIVE')
