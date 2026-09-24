from pathlib import Path
import sys,re as _re

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# 1) Faster full-cycle data: second source is optional now, so never block on Stooq synchronously.
p=root/'veritas_intelligence.py'
s=p.read_text(encoding='utf-8')
if "OPTIONAL_SECONDARY_DISABLED_IN_FAST_LOOP" not in s:
    s += r'''

# Optional secondary verification must not add 15-30 seconds to the fast decision loop.
# It may be restored later as an asynchronous diagnostic lane.
def _stooq_latest(symbol):
    raise RuntimeError('OPTIONAL_SECONDARY_DISABLED_IN_FAST_LOOP')
'''
# Refresh short Yahoo bars more aggressively for the movement detector.
s=s.replace("ttl=45 if interval in ('1m','2m','5m') else 240 if interval in ('15m','30m','60m','1h') else 1800",
            "ttl=15 if interval in ('1m','2m','5m') else 90 if interval in ('15m','30m','60m','1h') else 1800",1)

# Brent miss repair: near-qualified local impulse can seed Movement Genesis before the slow committee flips.
if "_movement_genesis_base_v2" not in s:
    s += r'''

_movement_genesis_base_v2=movement_genesis_engine
def movement_genesis_engine(asset,horizon,f,raw,research_decision,trade_plan):
    out=_movement_genesis_base_v2(asset,horizon,f,raw,research_decision,trade_plan)
    if out.get('eligible'):
        out['micro_impulse_bridge']=False
        return out
    pb=f.get('impulse_pivot_break') or {}
    cand=str(pb.get('candidate_direction') or pb.get('direction') or 'NO_TRADE')
    ev=pb.get('evidence') or {}
    local_eff=float(pb.get('local_efficiency') or 0.0)
    local_vol=float(pb.get('local_volume_ratio') or 0.0)
    z3=abs(float(pb.get('z3') or 0.0))
    conf=int(pb.get('confirmations') or 0)
    broken=bool(ev.get('local_resistance_break') or ev.get('local_support_break'))
    impulse=bool(ev.get('positive_impulse') or ev.get('negative_impulse'))
    micro_ok=bool(horizon in ('1h','4h') and cand in ('LONG','SHORT') and broken and impulse
                  and conf>=4 and local_eff>=0.50 and (local_vol>=1.05 or z3>=0.80))
    if not micro_ok:
        out['micro_impulse_bridge']=False
        return out
    f2=dict(f);pb2=dict(pb);ev2=dict(ev)
    pb2.update({'active':True,'direction':cand,'evidence':ev2,
                'reason':'micro_impulse_bridge'})
    f2['impulse_pivot_break']=pb2
    bridged=_movement_genesis_base_v2(asset,horizon,f2,raw,research_decision,trade_plan)
    bridged['micro_impulse_bridge']=True
    bridged['micro_impulse_inputs']={'confirmations':conf,'local_efficiency':local_eff,
                                     'local_volume_ratio':local_vol,'z3':z3}
    if bridged.get('eligible'):
        bridged['reason']='qualified_micro_impulse_'+str(bridged.get('state','move_start')).lower()
    return bridged
'''
p.write_text(s,encoding='utf-8')

# 2) Signal carries a real executable TP, not a UI-only target.
p=root/'veritas_v85/domain.py'
s=p.read_text(encoding='utf-8')
old="""    probability_source: str | None = None
    movement_state: str | None = None

    def __post_init__(self) -> None:
"""
new="""    probability_source: str | None = None
    movement_state: str | None = None
    take_profit_price: Decimal | None = None

    def __post_init__(self) -> None:
"""
if old not in s: raise SystemExit('TP_SIGNAL_FIELD_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
old="""        if self.entry_probability is not None:
            ep=decimal(self.entry_probability, nonnegative=True)
            if ep > ONE: raise ValueError("Signal entry probability above one")
            object.__setattr__(self, "entry_probability", ep)
"""
new="""        if self.entry_probability is not None:
            ep=decimal(self.entry_probability, nonnegative=True)
            if ep > ONE: raise ValueError("Signal entry probability above one")
            object.__setattr__(self, "entry_probability", ep)
        if self.take_profit_price is not None:
            object.__setattr__(self, "take_profit_price", decimal(self.take_profit_price, positive=True))
"""
if old not in s: raise SystemExit('TP_SIGNAL_VALIDATE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 3) Routing persists the selected target into the executable Signal.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="""        movement_state=str((r.get('movement_genesis') or {}).get('state') or 'IDLE')
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION'))
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
new="""        movement_state=str((r.get('movement_genesis') or {}).get('state') or 'IDLE')
        validated_add=bool(fam=='MOVEMENT_GENESIS' and movement_state in ('CONFIRMED','ACCELERATION'))
        tp_raw=p.get('tactical_target_price') or p.get('target_price')
        if tp_raw is None:
            tp_raw=(r.get('movement_genesis') or {}).get('target_price')
        if tp_raw is None:
            rr=r.get('range_retest_breakout') or {}
            if rr.get('direction')==d or rr.get('candidate_direction')==d: tp_raw=rr.get('target_price')
        take_profit=None
        try:
            tp=decimal(tp_raw,positive=True); px=decimal(r.get('price'),positive=True)
            if (d=='LONG' and tp>px) or (d=='SHORT' and tp<px): take_profit=tp
        except Exception:
            take_profit=None
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
if old not in s: raise SystemExit('TP_ROUTE_TARGET_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
old="""                          entry_probability=D(str(prob)),probability_source=prob_source,movement_state=movement_state)
"""
new="""                          entry_probability=D(str(prob)),probability_source=prob_source,
                          movement_state=movement_state,take_profit_price=take_profit)
"""
if old not in s: raise SystemExit('TP_ROUTE_SIGNAL_ARGS_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 4) Execution: persist TP, execute it on the fast guard lane, and add a net-positive profit lock.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')
old="""        payload = {"position":position_dict(pos),"signal":asdict(signal),"entry_nav":str(nav),
                   "entry_fx":str(quote.fx_to_nav),"instrument_multiplier":str(self.instruments[signal.asset].multiplier),"entry_fee":str(fee),"scope":"SYNTHETIC_PAPER",
"""
new="""        payload = {"position":position_dict(pos),"signal":asdict(signal),"entry_nav":str(nav),
                   "take_profit_price":str(signal.take_profit_price) if signal.take_profit_price is not None else None,
                   "entry_fx":str(quote.fx_to_nav),"instrument_multiplier":str(self.instruments[signal.asset].multiplier),"entry_fee":str(fee),"scope":"SYNTHETIC_PAPER",
"""
if old not in s: raise SystemExit('TP_OPEN_PAYLOAD_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

old="""                intent = manage(existing,signal,quote,self.instruments[asset],at,
                                portfolio_hard_stop=portfolio_hard_stop,thesis_break=thesis_break,
                                pending_exit_reason=pending["reason"] if pending else None)
"""
new="""                # Bind one executable TP to the episode. UI targets never have execution authority.
                ep_tp_row=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(existing.episode_id,)).fetchone()
                ep_tp=json.loads(ep_tp_row['payload']) if ep_tp_row else {}
                tp_raw=ep_tp.get('take_profit_price')
                tp=None
                try: tp=decimal(tp_raw,positive=True) if tp_raw not in (None,'') else None
                except Exception: tp=None
                if (tp is None and signal is not None and signal.direction==existing.direction
                        and signal.horizon==existing.horizon and signal.idea_id==existing.idea_id
                        and signal.take_profit_price is not None):
                    tp=signal.take_profit_price
                    ep_tp['take_profit_price']=str(tp)
                    c.execute('UPDATE v85_episodes SET payload=? WHERE episode_id=?',
                              (canonical_json(ep_tp),existing.episode_id))
                intent = manage(existing,signal,quote,self.instruments[asset],at,
                                portfolio_hard_stop=portfolio_hard_stop,thesis_break=thesis_break,
                                pending_exit_reason=pending["reason"] if pending else None)
                if intent.action == Action.HOLD and quote is not None and tp is not None:
                    tp_hit=(quote.price>=tp if existing.direction==Direction.LONG else quote.price<=tp)
                    if tp_hit:
                        reason='TAKE_PROFIT'
                        iid=stable_id('INT_',existing.episode_id,existing.revision,Action.EXIT.value,
                                      reason,quote.event_id,existing.policy_version)
                        intent=replace(intent,intent_id=iid,action=Action.EXIT,reason=reason)
"""
if old not in s: raise SystemExit('TP_MANAGE_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Profit Harvest 2.0: ratchet the structural stop into net-positive territory once MFE is meaningful.
old="""                    stage=(signal.movement_state if signal is not None else None)
                    harvest_ratio=(D('.60') if stage=='ACCELERATION' else D('.45') if stage=='CONFIRMED' else D('.40'))
                    giveback_trigger=max(D('.0015'),mfe*harvest_ratio)
"""
new="""                    stage=(signal.movement_state if signal is not None else None)
                    harvest_ratio=(D('.60') if stage=='ACCELERATION' else D('.45') if stage=='CONFIRMED' else D('.40'))
                    giveback_trigger=max(D('.0015'),mfe*harvest_ratio)
                    if mfe>=mfe_trigger and current_ret>all_in_floor*D('1.50'):
                        lock_share=(D('.35') if stage=='ACCELERATION' else D('.30') if stage=='CONFIRMED' else D('.25'))
                        desired_lock=max(all_in_floor*D('1.25'),mfe*lock_share)
                        max_lock=max(ZERO,current_ret-D('.0005'))
                        lock_ret=min(desired_lock,max_lock)
                        if lock_ret>all_in_floor*D('1.05'):
                            lock_stop=(existing.entry_price*(ONE+lock_ret) if existing.direction==Direction.LONG
                                       else existing.entry_price*(ONE-lock_ret))
                            improves=(lock_stop>existing.stop_price if existing.direction==Direction.LONG
                                      else lock_stop<existing.stop_price)
                            still_safe=(lock_stop<quote.price if existing.direction==Direction.LONG
                                        else lock_stop>quote.price)
                            if improves and still_safe:
                                existing=replace(existing,stop_price=lock_stop,revision=existing.revision+1)
                                c.execute('UPDATE v85_positions SET payload=? WHERE account_id=? AND asset=?',
                                          (canonical_json(position_dict(existing)),existing.account_id,existing.asset))
                                ep_lock=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(existing.episode_id,)).fetchone()
                                if ep_lock:
                                    pay_lock=json.loads(ep_lock['payload']);pay_lock['position']=position_dict(existing)
                                    pay_lock.setdefault('profit_lock_events',[]).append({
                                      'at':at.isoformat(),'event_id':quote.event_id,'mfe':str(mfe),
                                      'current_return':str(current_ret),'locked_return':str(lock_ret),
                                      'stop_price':str(lock_stop),'stage':stage})
                                    c.execute('UPDATE v85_episodes SET payload=? WHERE episode_id=?',
                                              (canonical_json(pay_lock),existing.episode_id))
"""
if old not in s: raise SystemExit('PROFIT_LOCK_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)

# Surface the executable TP in portfolio reports.
p.write_text(s,encoding='utf-8')

p=root/'veritas_v85/application.py'
s=p.read_text(encoding='utf-8')
old="""                    ep=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(pos.episode_id,)).fetchone()
                    fc=accrued(pos,json.loads(ep['payload']),at) if ep else D('0');carry_cost+=fc
                    unreal+=u;nominal+=pos.quantity*price*fx;valid=valid and checked
                    positions.append({**json.loads(x['payload']),'mark':str(price),'unrealized_pnl':str(u),'mark_verified':checked})
"""
new="""                    ep=c.execute('SELECT payload FROM v85_episodes WHERE episode_id=?',(pos.episode_id,)).fetchone()
                    ep_payload=json.loads(ep['payload']) if ep else {}
                    fc=accrued(pos,ep_payload,at) if ep else D('0');carry_cost+=fc
                    unreal+=u;nominal+=pos.quantity*price*fx;valid=valid and checked
                    positions.append({**json.loads(x['payload']),'mark':str(price),'unrealized_pnl':str(u),'mark_verified':checked,
                                      'take_price':ep_payload.get('take_profit_price')})
"""
if old not in s: raise SystemExit('TP_PORTFOLIO_REPORT_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

# 5) Fast Market Pulse: refresh existing-position marks every guard cycle for all seven assets.
p=root/'veritas_v85/quote_guard.py'
s=p.read_text(encoding='utf-8')
s=s.replace("GUARD_SUPPORTED_ASSETS=frozenset(('BTC','ETH','NQ'))",
            "GUARD_SUPPORTED_ASSETS=frozenset(('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'))")
s=s.replace("GUARD_SUPPORTED_ASSETS=frozenset(('BTC','ETH','NDX'))",
            "GUARD_SUPPORTED_ASSETS=frozenset(('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF'))")

if 'def collect_one_source_market(' not in s:
    insert=r'''
def _yahoo_fast_last(symbol):
    err=None
    for interval in ('1m','5m'):
        try:
            with httpx.Client(timeout=httpx.Timeout(5.0,connect=2.5),
                              headers={'User-Agent':'Mozilla/5.0 VERITAS fast-pulse'}) as client:
                r=client.get('https://query1.finance.yahoo.com/v8/finance/chart/'+symbol,
                             params={'range':'1d' if interval=='1m' else '5d','interval':interval,
                                     'includePrePost':'true'})
                r.raise_for_status();res=r.json()['chart']['result'][0]
            q=res['indicators']['quote'][0];ts=res.get('timestamp') or []
            closes=q.get('close') or []
            for i in range(len(ts)-1,-1,-1):
                if i<len(closes) and closes[i] is not None:
                    return D(str(closes[i])),datetime.fromtimestamp(int(ts[i]),timezone.utc),interval
        except Exception as ex: err=ex
    raise RuntimeError('FAST_YAHOO_QUOTE_FAIL:'+type(err).__name__ if err else 'FAST_YAHOO_QUOTE_FAIL')

def collect_one_source_market(app,asset):
    model=app.model
    if asset=='NQ':
        price,pt,itv=_yahoo_fast_last('NQ%3DF');max_age=900;source='Yahoo CME NQ fast pulse'
        market_open=model._futures_market_open_from_age(pt.isoformat())
    elif asset=='BRENT':
        price,pt,itv=_yahoo_fast_last('BZ%3DF');max_age=max(900,int(getattr(model,'DELAYED_FUTURES_MAX_AGE_SECONDS',1800)));source='Yahoo Brent fast pulse'
        market_open=model._futures_market_open_from_age(pt.isoformat())
    elif asset=='GOLD':
        price,pt,itv=_yahoo_fast_last('GC%3DF');max_age=max(900,int(getattr(model,'DELAYED_FUTURES_MAX_AGE_SECONDS',1800)));source='Yahoo Gold fast pulse'
        market_open=model._futures_market_open_from_age(pt.isoformat())
    elif asset=='MOEX':
        z=model._moex_current_quote();price=D(str(z['price']));pt=utc(z['observed_at']);max_age=int(getattr(model,'MOEX_MAX_AGE_SECONDS',1200));source='MOEX ISS IMOEX fast pulse'
        market_open=model._moex_index_open_now()
    elif asset=='CNYRUBF':
        z=model._moex_futures_current_quote('CNYRUBF');price=D(str(z['price']));pt=utc(z['observed_at']);max_age=3600;source='MOEX ISS CNYRUBF fast pulse'
        market_open=model._futures_market_open_from_age(pt.isoformat())
    else:
        raise ValueError('UNSUPPORTED_FAST_PULSE_ASSET')
    return Quote(asset,price,stable_id('QFAST_',asset,str(price),pt),pt,None,
                 source,None,True,bool(market_open),max_age,quote_currency='RUB')
'''
    anchor="def refresh_and_guard(app):\n"
    if anchor not in s: raise SystemExit('FAST_PULSE_GUARD_ANCHOR_NOT_FOUND')
    s=s.replace(anchor,insert+'\n'+anchor,1)

pat=r"""            if a in \('BTC','ETH'\):updates\[a\]=collect_crypto\(a\)
            .*?
            elif a=='(?:NQ|NDX)':updates\[a\]=collect_ndx\(app\.model\)
"""
new="""    def _fast_fetch(a):
        if a in ('BTC','ETH'): return a,collect_crypto(a)
        return a,collect_one_source_market(app,a)
    workers=max(1,min(7,len(assets)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures={pool.submit(_fast_fetch,a):a for a in sorted(assets)}
        for fut in as_completed(futures):
            a=futures[fut]
            try:
                key,q=fut.result();updates[key]=q
            except Exception as exc:
                app.last_error='quote_guard '+a+': '+type(exc).__name__
"""
s,n=_re.subn(pat,new,s,count=1,flags=_re.S)
if n!=1: raise SystemExit('FAST_PULSE_REFRESH_ANCHOR_NOT_FOUND:'+str(n))
p.write_text(s,encoding='utf-8')

# 6) Durable Movement State Memory with hysteresis.
p=root/'veritas_v86/application.py'
s=p.read_text(encoding='utf-8')
if 'def apply_movement_state_memory(' not in s:
    helper=r'''
_STATE_RANK={'IDLE':0,'WATCH':1,'MOVE_START':2,'CONFIRMED':3,'ACCELERATION':4,'EXHAUSTION':0}

def apply_movement_state_memory(ledger,summary,at):
    at=utc(at);out=[]
    with ledger.transaction() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS v86_movement_state(
          asset TEXT NOT NULL,horizon TEXT NOT NULL,direction TEXT NOT NULL,state TEXT NOT NULL,
          score TEXT NOT NULL,hits INTEGER NOT NULL,misses INTEGER NOT NULL,last_price TEXT,
          last_at TEXT NOT NULL,payload TEXT NOT NULL,PRIMARY KEY(asset,horizon))""")
        for row in summary:
            mg=dict(row.get('movement_genesis') or {})
            asset=str(row.get('asset') or '');h=str(row.get('horizon') or '')
            if h not in ('1h','4h','1d'):
                out.append(row);continue
            prior=c.execute('SELECT * FROM v86_movement_state WHERE asset=? AND horizon=?',(asset,h)).fetchone()
            pd=dict(prior) if prior else None
            raw_state=str(mg.get('state') or 'IDLE');direction=str(mg.get('direction') or 'NO_TRADE')
            score=float(mg.get('score') or 0.0);eligible=bool(mg.get('eligible') and direction in ('LONG','SHORT'))
            hits=1 if eligible else 0;misses=0 if eligible else 1;state=raw_state
            same=False;age=None
            if pd:
                try: age=(at-utc(pd['last_at'])).total_seconds()
                except Exception: age=None
                same=bool(pd.get('direction')==direction and direction in ('LONG','SHORT') and age is not None and 0<=age<=180)
            if eligible and same:
                hits=int(pd.get('hits') or 0)+1;misses=0
                old_state=str(pd.get('state') or 'IDLE')
                if _STATE_RANK.get(old_state,0)>_STATE_RANK.get(state,0) and score>=0.55:
                    state=old_state
                if state=='MOVE_START' and hits>=2 and score>=0.60:
                    state='CONFIRMED'
                if state=='CONFIRMED' and hits>=3 and score>=0.78:
                    state='ACCELERATION'
                mg['state']=state
                mg['target_fraction']={'MOVE_START':0.05,'CONFIRMED':0.10,'ACCELERATION':0.25}.get(state,mg.get('target_fraction',0.0))
                mg['eligible']=state in ('MOVE_START','CONFIRMED','ACCELERATION')
            elif not eligible and pd and age is not None and 0<=age<=90:
                misses=int(pd.get('misses') or 0)+1
                # Do not create a new entry from memory alone, but preserve state for diagnostics/management.
                mg['memory_prior_state']=pd.get('state');mg['memory_prior_direction']=pd.get('direction')
            mg['raw_state']=raw_state;mg['memory_hits']=hits;mg['memory_misses']=misses;mg['memory_persisted']=True
            store_dir=direction if direction in ('LONG','SHORT') else (pd.get('direction') if pd else 'NO_TRADE')
            store_state=state if eligible else (pd.get('state') if pd and misses<=2 else raw_state)
            payload=canonical_json({'movement_genesis':mg,'price':row.get('price'),'regime':row.get('regime')})
            c.execute("""INSERT INTO v86_movement_state(asset,horizon,direction,state,score,hits,misses,last_price,last_at,payload)
                         VALUES(?,?,?,?,?,?,?,?,?,?)
                         ON CONFLICT(asset,horizon) DO UPDATE SET direction=excluded.direction,state=excluded.state,
                         score=excluded.score,hits=excluded.hits,misses=excluded.misses,last_price=excluded.last_price,
                         last_at=excluded.last_at,payload=excluded.payload""",
                      (asset,h,store_dir,store_state,str(score),hits,misses,str(row.get('price')),at.isoformat(),payload))
            row['movement_genesis']=mg;out.append(row)
    return out
'''
    anchor="class Application(V85Application):\n"
    if anchor not in s: raise SystemExit('MOVEMENT_MEMORY_CLASS_ANCHOR_NOT_FOUND')
    s=s.replace(anchor,helper+'\n\n'+anchor,1)

old="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
"""
new="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        summary=apply_movement_state_memory(self.ledger,summary,now())
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
"""
if old not in s: raise SystemExit('MOVEMENT_MEMORY_COMMIT_ANCHOR_NOT_FOUND')
s=s.replace(old,new,1)
p.write_text(s,encoding='utf-8')

print('V86_TP_PULSE_MEMORY_ACTIVE')
