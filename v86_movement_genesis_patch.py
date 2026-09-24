from pathlib import Path
import sys,re as _re

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# 1) Movement Genesis V1: detect the birth, confirmation and acceleration of a move.
p=root/'veritas_intelligence.py'
s=p.read_text(encoding='utf-8')

engine=r'''
def movement_genesis_engine(asset,horizon,f,raw,research_decision,trade_plan):
    """Early movement detector independent from the slow committee direction.

    This is an execution candidate generator, not a claim of calibrated probability.
    It uses price structure, impulse, volume, path efficiency and higher-horizon agreement.
    """
    p=float(f.get('price') or 0.0)
    if p<=0 or horizon not in ('1h','4h','1d'):
        return {'state':'IDLE','eligible':False,'direction':'NO_TRADE','score':0.0,'reason':'unsupported_horizon_or_price'}

    st=f.get('intraday_structure') or {}
    hs=f.get('horizon_structure') or {}
    ti=f.get('trend_impulse') or {}
    pb=f.get('impulse_pivot_break') or {}
    rs=f.get('range_retest_breakout') or {}
    lev=f.get('structural_levels') or {}

    direction='NO_TRADE'; trigger='NONE'
    if pb.get('active') and pb.get('direction') in ('LONG','SHORT'):
        direction=str(pb.get('direction')); trigger='IMPULSE_PIVOT_BREAK'
    elif st.get('fresh_breakout') and st.get('direction') in ('LONG','SHORT'):
        direction=str(st.get('direction')); trigger='FRESH_BREAKOUT'
    elif research_decision in ('LONG','SHORT') and str(ti.get('direction') or '')==research_decision:
        direction=str(research_decision); trigger='COMMITTEE_PLUS_TREND'
    else:
        hd=str(hs.get('direction') or hs.get('raw_direction') or '')
        hz=abs(float(hs.get('z') or 0.0))
        if hd in ('LONG','SHORT') and hz>=1.10:
            direction=hd; trigger='HORIZON_MOMENTUM'

    if direction not in ('LONG','SHORT'):
        return {'state':'IDLE','eligible':False,'direction':'NO_TRADE','score':0.0,'reason':'no_directional_genesis'}

    hs_dir=str(hs.get('direction') or hs.get('raw_direction') or '')
    hs_score=float(hs.get('score') or 0.0)
    hs_same=bool(hs_dir==direction and hs_score>=0.42)
    trend_same=bool(str(ti.get('direction') or '')==direction and str(ti.get('phase') or '') in
                    ('EARLY_TREND','IMPULSE_TREND','TREND_DAY'))
    st_same=bool(str(st.get('direction') or '')==direction)
    fresh=bool(st.get('fresh_breakout') or st.get('breakout_hold') or
               (pb.get('active') and pb.get('direction')==direction))
    volume=max(float(st.get('relative_volume') or 0.0),float(pb.get('local_volume_ratio') or 0.0))
    efficiency=max(float(st.get('session_efficiency') or 0.0),float(pb.get('local_efficiency') or 0.0))
    persistence=max(float(st.get('session_persistence') or 0.0),float(hs.get('persistence') or 0.0))
    hz=abs(float(hs.get('z') or 0.0))
    pb_conf=int(pb.get('confirmations') or 0)

    evidence={
      'breakout_or_impulse':fresh,
      'volume':volume>=1.10,
      'path_efficiency':efficiency>=0.32,
      'horizon_alignment':hs_same,
      'trend_phase_alignment':trend_same,
      'persistence_or_z':persistence>=0.55 or hz>=1.20 or pb_conf>=4,
    }
    count=sum(bool(v) for v in evidence.values())
    score=clip(0.10+0.12*count+0.08*min(2.0,max(0.0,volume-0.8))+
               0.10*min(1.0,efficiency)+0.08*min(2.0,hz)/2.0,0.0,1.0)

    strong_trigger=bool((pb.get('active') and pb_conf>=4) or
                        (st.get('fresh_breakout') and volume>=1.10) or
                        (trend_same and hz>=1.20))
    state='WATCH'
    if count>=5 and volume>=1.45 and efficiency>=0.45 and (pb_conf>=5 or hz>=1.45 or str(ti.get('phase') or '') in ('IMPULSE_TREND','TREND_DAY')):
        state='ACCELERATION'
    elif count>=4 and strong_trigger and (hs_same or trend_same):
        state='CONFIRMED'
    elif count>=3 and strong_trigger:
        state='MOVE_START'
    elif st.get('false_breakout') and volume<1.0:
        state='EXHAUSTION'

    atr=float(st.get('atr_5m') or 0.0)
    horizon_floor={'1h':0.0015,'4h':0.0025,'1d':0.0040}.get(horizon,0.0015)
    sigma=float(ti.get('sigma_1h') or 0.0)
    horizon_scale={'1h':1.0,'4h':4.0,'1d':8.0}.get(horizon,1.0)
    noise_floor=max(horizon_floor,0.85*sigma*math.sqrt(horizon_scale))
    noise=max(atr*0.20,p*noise_floor*0.35)

    stop=None
    if pb.get('direction')==direction and pb.get('stop_price') is not None:
        try: stop=float(pb.get('stop_price'))
        except Exception: stop=None
    if stop is None:
        tp=trade_plan or {}
        try:
            candidate=float(tp.get('stop_price')) if tp.get('stop_price') is not None else None
        except Exception:
            candidate=None
        if candidate and ((direction=='LONG' and candidate<p) or (direction=='SHORT' and candidate>p)):
            stop=candidate

    if stop is None:
        anchors=[]
        for x in (lev.get('support') if direction=='LONG' else lev.get('resistance'),
                  st.get('recent_swing_anchor'),st.get('breakout_level'),st.get('invalidation_price')):
            try:
                x=float(x)
                if (direction=='LONG' and x<p) or (direction=='SHORT' and x>p): anchors.append(x)
            except Exception:
                pass
        if anchors:
            anchor=max(anchors) if direction=='LONG' else min(anchors)
            stop=anchor-noise if direction=='LONG' else anchor+noise
        else:
            stop=p*(1-noise_floor*1.10) if direction=='LONG' else p*(1+noise_floor*1.10)

    stop_dist=abs(p-stop)/p if p else 999.0
    if stop_dist<noise_floor:
        stop=p*(1-noise_floor*1.05) if direction=='LONG' else p*(1+noise_floor*1.05)
        stop_dist=abs(p-stop)/p

    target=None
    for src in (pb,rs):
        if src.get('direction')==direction and src.get('target_price') is not None:
            try:
                t=float(src.get('target_price'))
                if (direction=='LONG' and t>p) or (direction=='SHORT' and t<p):
                    target=t; break
            except Exception:
                pass
    if target is None:
        try:
            lvl=float(lev.get('resistance') if direction=='LONG' else lev.get('support'))
            if (direction=='LONG' and lvl>p) or (direction=='SHORT' and lvl<p): target=lvl
        except Exception:
            target=None
    projected=max(0.0060,2.20*stop_dist)
    fallback=p*(1+projected) if direction=='LONG' else p*(1-projected)
    if target is None:
        target=fallback
    elif direction=='LONG':
        target=max(target,fallback)
    else:
        target=min(target,fallback)

    reward=abs(target-p)/p
    all_in_cost=0.0012
    net_reward=max(0.0,reward-all_in_cost)
    net_risk=stop_dist+all_in_cost
    net_rr=net_reward/max(net_risk,1e-12)
    target_fraction={'MOVE_START':0.05,'CONFIRMED':0.10,'ACCELERATION':0.25}.get(state,0.0)
    eligible=bool(state in ('MOVE_START','CONFIRMED','ACCELERATION') and
                  count>=3 and stop_dist>=noise_floor and net_rr>=1.25)

    return {'version':'movement-genesis-v1','state':state,'eligible':eligible,'direction':direction,
            'score':round(score,4),'trigger':trigger,'confirmation_count':count,'evidence':evidence,
            'volume_ratio':round(volume,3),'path_efficiency':round(efficiency,4),
            'persistence':round(persistence,4),'horizon_z':round(hz,3),
            'stop_price':stop,'target_price':target,'stop_distance_pct':stop_dist,
            'noise_floor_pct':noise_floor,'gross_reward_pct':reward,'net_reward_pct':net_reward,
            'net_rr':net_rr,'target_fraction':target_fraction,
            'scale_stage':state,'calibrated_probability':None,
            'reason':'qualified_'+state.lower() if eligible else 'movement_watch_'+state.lower()}
'''

if 'def movement_genesis_engine(' not in s:
    s += '\n\n'+engine+'\n'

anchor="                made += 1\n                z = {'asset': asset, 'horizon': horizon, 'decision': dec,\n"
if anchor not in s: raise SystemExit('MOVEMENT_SUMMARY_ANCHOR_NOT_FOUND')
replacement="""                movement_genesis=movement_genesis_engine(asset,horizon,f,raw,research_dec,trade_plan)
                f['movement_genesis']=movement_genesis
                made += 1
                z = {'asset': asset, 'horizon': horizon, 'decision': dec,
"""
s=s.replace(anchor,replacement,1)

old="'tactical_reversal':tactical_reversal,'range_retest_breakout':f.get('range_retest_breakout') or {},'impulse_pivot_break':f.get('impulse_pivot_break') or {},'structural_levels':f.get('structural_levels') or {},"
new="'tactical_reversal':tactical_reversal,'range_retest_breakout':f.get('range_retest_breakout') or {},'impulse_pivot_break':f.get('impulse_pivot_break') or {},'movement_genesis':movement_genesis,'structural_levels':f.get('structural_levels') or {},"
if old not in s: raise SystemExit('MOVEMENT_SUMMARY_FIELD_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 2) Signal contract carries movement state so the execution book can scale and harvest.
p=root/'veritas_v85/domain.py'
s=p.read_text(encoding='utf-8')
old="    probability_source: str | None = None\n\n    def __post_init__(self) -> None:\n"
new="    probability_source: str | None = None\n    movement_state: str | None = None\n\n    def __post_init__(self) -> None:\n"
if old not in s: raise SystemExit('MOVEMENT_SIGNAL_FIELD_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 3) Route strong movement candidates even when the slow committee is still neutral.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')

old="""def family(row):
    p=row.get('trade_plan') or {}
"""
new="""def family(row):
    p=row.get('trade_plan') or {}
    mg=row.get('movement_genesis') or {}
    if mg.get('eligible') and mg.get('direction') in ('LONG','SHORT'):
        return 'MOVEMENT_GENESIS'
"""
if old not in s: raise SystemExit('MOVEMENT_FAMILY_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="        d=str(r.get('research_decision') or 'NO_TRADE')\n"
new="""        base_d=str(r.get('research_decision') or 'NO_TRADE')
        mg=r.get('movement_genesis') or {}
        move_d=str(mg.get('direction') or 'NO_TRADE') if mg.get('eligible') else 'NO_TRADE'
        d=move_d if move_d in ('LONG','SHORT') else base_d
        r['_route_direction']=d
        r['_movement_route']=bool(move_d in ('LONG','SHORT'))
"""
if old not in s: raise SystemExit('MOVEMENT_ROUTE_DIRECTION_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="        rank=conf+0.10*score+0.03*independent\n"
new="""        mg_score=max(0,min(1,fnum((r.get('movement_genesis') or {}).get('score'))))
        rank=conf+0.10*score+0.03*independent+0.20*mg_score
"""
if old not in s: raise SystemExit('MOVEMENT_ROUTE_RANK_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="        support={d:sum(w for r,w in items if r['research_decision']==d) for d in ['LONG','SHORT']}\n"
new="        support={d:sum(w for r,w in items if r.get('_route_direction')==d) for d in ['LONG','SHORT']}\n"
if old not in s: raise SystemExit('MOVEMENT_SUPPORT_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
old="        ranked=sorted(((r,w) for r,w in items if r['research_decision']==d),key=lambda rw:(-rw[1],HORIZONS.index(rw[0]['horizon'])))\n"
new="        ranked=sorted(((r,w) for r,w in items if r.get('_route_direction')==d),key=lambda rw:(-rw[1],HORIZONS.index(rw[0]['horizon'])))\n"
if old not in s: raise SystemExit('MOVEMENT_RANKED_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""    tr=row.get('tactical_reversal') or {}
    if not setup and tr.get('active') and tr.get('direction')==direction:
"""
new="""    tr=row.get('tactical_reversal') or {}
    mg=row.get('movement_genesis') or {}
    if mg.get('eligible') and mg.get('direction')==direction and mg.get('state') in ('MOVE_START','CONFIRMED','ACCELERATION'):
        max_fraction=D(str(mg.get('target_fraction') or '.05'))
        return {'eligible':True,'reason':'MOVEMENT_GENESIS_'+str(mg.get('state')),
                'max_fraction':max_fraction,'movement':True,'movement_state':mg.get('state'),
                'movement_score':mg.get('score'),'net_rr':mg.get('net_rr'),
                'independent':mg.get('confirmation_count'),'historical_edge_gate':'FAIL'}
    if not setup and tr.get('active') and tr.get('direction')==direction:
"""
if old not in s: raise SystemExit('MOVEMENT_WINRATE_GATE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""            pp=dict(rr.get('trade_plan') or {})
            pp['winrate_repair_gate']=gate
            executable.append((rr,ww,pp,gate))
"""
new="""            pp=dict(rr.get('trade_plan') or {})
            if gate.get('movement'):
                mg=rr.get('movement_genesis') or {}
                pp.update({'eligible':True,'reason':'movement_genesis','setup':'MOVEMENT_GENESIS',
                           'stop_price':mg.get('stop_price'),'tactical_target_price':mg.get('target_price'),
                           'expected_move_pct':mg.get('gross_reward_pct'),
                           'net_expected_move_pct':mg.get('net_reward_pct'),
                           'net_expected_to_stop_ratio':mg.get('net_rr'),
                           'noise_floor_stop_distance_pct':mg.get('noise_floor_pct'),
                           'initial_position_fraction':0.05})
            pp['winrate_repair_gate']=gate
            executable.append((rr,ww,pp,gate))
"""
if old not in s: raise SystemExit('MOVEMENT_EXECUTABLE_PLAN_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""        # Repair mode is deliberately conservative: 5% research probe only.
        desired=min(D('.05'),gate.get('max_fraction') or D('.05'))
        traces.append({'asset':a,'direction':d,'horizon':h,'status':'ROUTED_REPAIR_PROBE',
"""
new="""        desired=min(D('.25'),gate.get('max_fraction') or D('.05'))
        route_status=('ROUTED_MOVEMENT_'+str(gate.get('movement_state')) if gate.get('movement') else 'ROUTED_REPAIR_PROBE')
        traces.append({'asset':a,'direction':d,'horizon':h,'status':route_status,
"""
if old not in s: raise SystemExit('MOVEMENT_DESIRED_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""        did=str(r.get('decision_id') or stable_id('D85_',a,d,h,at,r.get('score'),p.get('stop_price')))
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
new="""        did=str(r.get('decision_id') or stable_id('D85_',a,d,h,at,r.get('score'),p.get('stop_price')))
        movement_state=str((r.get('movement_genesis') or {}).get('state') or 'IDLE')
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION'))
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
if old not in s: raise SystemExit('MOVEMENT_SIGNAL_CREATION_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="                          confirmations=tuple(unique.values()),validated_add=False,\n                          entry_probability=D(str(prob)),probability_source=prob_source)\n"
new="                          confirmations=tuple(unique.values()),validated_add=validated_add,\n                          entry_probability=D(str(prob)),probability_source=prob_source,movement_state=movement_state)\n"
if old not in s: raise SystemExit('MOVEMENT_SIGNAL_ARGS_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 4) Dynamic scale-in: 5% initial -> 10% confirmed -> 25% acceleration.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')

anchor="    def _open(self,c,account_id: str,signal: Signal,quote: Quote,nav: Decimal,admission,account,at: datetime) -> dict:\n"
if anchor not in s: raise SystemExit('MOVEMENT_BOOK_OPEN_ANCHOR_NOT_FOUND')
method=r'''    def _scale_in(self,c,p: Position,signal: Signal,quote: Quote,quotes: Mapping[str,Quote],
                  at: datetime,limits: RiskLimits):
        if signal.direction!=p.direction or signal.idea_id!=p.idea_id or signal.validated_add is not True:
            return None,'SCALE_NOT_VALIDATED'
        if signal.movement_state not in ('CONFIRMED','ACCELERATION'):
            return None,'SCALE_STAGE_NOT_CONFIRMED'
        nav,gross,valid,account=self._valuation(c,p.account_id,quotes,at)
        if not valid or nav<=0:
            return None,'SCALE_NAV_UNVERIFIED'
        spec=self.instruments[p.asset]
        current_notional=p.quantity*spec.multiplier*quote.price*quote.fx_to_nav
        current_fraction=current_notional/nav
        target=min(signal.desired_fraction,limits.asset_cap)
        desired_add=max(ZERO,target-current_fraction)
        if desired_add < limits.desired_step:
            return None,'SCALE_TARGET_REACHED'

        ratchet_stop=(max(p.stop_price,signal.stop_price) if p.direction==Direction.LONG
                      else min(p.stop_price,signal.stop_price))
        stop_distance=abs(quote.price-ratchet_stop)/quote.price
        existing_planned=current_fraction*(stop_distance+spec.commission_rate*(ONE+ratchet_stop/quote.price))
        remaining_stop=max(ZERO,limits.stop_risk_nav-existing_planned)
        add_limits=replace(limits,
                           remaining_gross=max(ZERO,limits.remaining_gross-gross),
                           asset_cap=max(ZERO,limits.asset_cap-current_fraction),
                           stop_risk_nav=remaining_stop,
                           soft_multiplier=ONE)
        add_signal=replace(signal,desired_fraction=desired_add,stop_price=ratchet_stop)
        admission=admit(add_signal,quote,spec,nav,add_limits,at)
        if not admission.allowed:
            return None,'SCALE_BLOCKED:'+admission.reason

        add_qty=admission.quantity
        total_qty=p.quantity+add_qty
        avg=(p.quantity*p.entry_price+add_qty*quote.price)/total_qty
        newpos=replace(p,quantity=total_qty,entry_price=avg,stop_price=ratchet_stop,revision=p.revision+1)

        row=c.execute('SELECT entry_fee FROM v85_positions WHERE account_id=? AND asset=?',
                      (p.account_id,p.asset)).fetchone()
        old_fee=D(row['entry_fee'])
        fee=admission.nominal*spec.commission_rate
        ep=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(p.episode_id,)).fetchone()
        payload=json.loads(ep['payload'])
        carry_now=accrued(p,payload,at)
        prior_funding=decimal(payload.get('legacy_realized_funding','0'),nonnegative=True)
        payload['legacy_realized_funding']=str(prior_funding+carry_now)
        payload['funding_start_at']=at.isoformat()
        payload['position']=position_dict(newpos)
        payload['entry_fee']=str(old_fee+fee)
        payload.setdefault('scale_events',[]).append({
          'at':at.isoformat(),'event_id':quote.event_id,'stage':signal.movement_state,
          'added_fraction':str(admission.fraction),'added_quantity':str(add_qty),
          'price':str(quote.price),'fee':str(fee),'target_fraction':str(target)})

        c.execute('UPDATE v85_positions SET payload=?,entry_fee=?,last_price=?,last_fx=? WHERE account_id=? AND asset=?',
                  (canonical_json(position_dict(newpos)),str(old_fee+fee),str(quote.price),str(quote.fx_to_nav),p.account_id,p.asset))
        c.execute('UPDATE v85_episodes SET payload=? WHERE episode_id=?',(canonical_json(payload),p.episode_id))
        c.execute('INSERT INTO v85_orders VALUES(?,?,?,?,?,?,?,?,?,?)',
                  (stable_id('INT_',p.episode_id,'ADD',p.revision+1,signal.decision_id,quote.event_id),
                   p.episode_id,p.account_id,quote.event_id,
                   'BUY' if p.direction==Direction.LONG else 'SHORT',
                   str(add_qty),str(quote.price),str(fee),'VALIDATED_SCALE_IN:'+str(signal.movement_state),at.isoformat()))
        c.execute('UPDATE v85_accounts SET realized_equity=? WHERE account_id=?',
                  (str(D(account['realized_equity'])-fee-carry_now),p.account_id))
        c.execute('INSERT INTO v85_path VALUES(?,?,?,?) ON CONFLICT(episode_id,event_id) DO NOTHING',
                  (p.episode_id,quote.event_id,at.isoformat(),str(quote.price)))
        self.fault_hook('after_scale_order')
        return {'action':'ADD','episode_id':p.episode_id,'quantity':str(add_qty),
                'total_quantity':str(total_qty),'price':str(quote.price),'avg_entry_price':str(avg),
                'fraction_added':str(admission.fraction),'target_fraction':str(target),
                'fee':str(fee),'reason':'VALIDATED_SCALE_IN:'+str(signal.movement_state)},'SCALED'

'''
if '    def _scale_in(' not in s:
    s=s.replace(anchor,method+anchor,1)

old="""        revised = replace(limits, remaining_gross=remaining)
        return admit(signal,quote,self.instruments[signal.asset],nav,revised,at),nav,account
"""
new="""        revised = replace(limits, remaining_gross=remaining)
        entry_signal=(replace(signal,desired_fraction=min(signal.desired_fraction,D('.05')))
                      if signal.setup_family=='MOVEMENT_GENESIS' else signal)
        return admit(entry_signal,quote,self.instruments[signal.asset],nav,revised,at),nav,account
"""
if old not in s: raise SystemExit('MOVEMENT_INITIAL_CAP_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""                action_reason = intent.reason
                if intent.action == Action.EXIT_PENDING_QUOTE:
"""
new="""                action_reason = intent.reason
                if intent.action == Action.HOLD and signal is not None and quote is not None and signal.validated_add:
                    scale_event,scale_reason=self._scale_in(c,existing,signal,quote,context,at,limits)
                    if scale_event is not None:
                        events.append(scale_event)
                        action_reason=scale_event['reason']
                    elif scale_reason not in ('SCALE_TARGET_REACHED','SCALE_NOT_VALIDATED','SCALE_STAGE_NOT_CONFIRMED'):
                        action_reason=scale_reason
                if intent.action == Action.EXIT_PENDING_QUOTE:
"""
if old not in s: raise SystemExit('MOVEMENT_SCALE_CALL_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Correct episode funding after scale-ins: carry already realized at each add must remain in final episode P&L.
old="        carry=decimal(original.get("legacy_realized_gross","0"))\n        episode_net=gross-fee-entry_fee-fund+carry\n"
new="""        carry=decimal(original.get("legacy_realized_gross","0"))
        prior_funding=decimal(original.get("legacy_realized_funding","0"),nonnegative=True)
        episode_net=gross-fee-entry_fee-fund-prior_funding+carry
"""
if old not in s: raise SystemExit('MOVEMENT_CLOSE_FUNDING_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
old="'funding_close_leg':str(fund),\n"
new="'funding_close_leg':str(fund+prior_funding),'funding_final_leg':str(fund),\n"
if old not in s: raise SystemExit('MOVEMENT_OUTCOME_FUNDING_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Profit Harvest V1: during acceleration allow more room; otherwise protect net-positive MFE.
old="                    giveback_trigger=max(D('.0015'),mfe*D('.40'))\n"
new="""                    stage=(signal.movement_state if signal is not None else None)
                    harvest_ratio=(D('.60') if stage=='ACCELERATION' else D('.45') if stage=='CONFIRMED' else D('.40'))
                    giveback_trigger=max(D('.0015'),mfe*harvest_ratio)
"""
if old not in s: raise SystemExit('MOVEMENT_HARVEST_RATIO_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 5) Portfolio specialization must be real, not just documented.
p=root/'veritas_v86/portfolios.py'
s=p.read_text(encoding='utf-8')
s,n=_re.subn(r'(PortfolioProfile\("Impulse".*?\()(.*?)(\),\n\s+max_gross=)',lambda m:m.group(1)+
             '"IMPULSE_GENESIS","IMPULSE_PIVOT_BREAK","RANGE_RETEST_BREAKOUT"'+m.group(3),s,count=1,flags=_re.S)
if n!=1: raise SystemExit('MOVEMENT_IMPULSE_PROFILE_PATCH_FAILED')
p.write_text(s,encoding='utf-8')

print('V86_MOVEMENT_GENESIS_ACTIVE')
