from pathlib import Path
import sys, json

root=Path(sys.argv[1] if len(sys.argv)>1 else '.').resolve()

# 1) Preserve entry probability in the immutable Signal/episode.
p=root/'veritas_v85/domain.py'
s=p.read_text(encoding='utf-8')
old="""    reentry_of_episode: str | None = None
    reentry_information_id: str | None = None

    def __post_init__(self) -> None:
"""
new="""    reentry_of_episode: str | None = None
    reentry_information_id: str | None = None
    entry_probability: Decimal | None = None
    probability_source: str | None = None

    def __post_init__(self) -> None:
"""
if old not in s: raise SystemExit('DOMAIN_SIGNAL_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
old="""        object.__setattr__(self, "confirmations", tuple(self.confirmations))
        if not all((self.asset, self.idea_id, self.decision_id)) or self.horizon not in HORIZON_SECONDS:
"""
new="""        object.__setattr__(self, "confirmations", tuple(self.confirmations))
        if self.entry_probability is not None:
            ep=decimal(self.entry_probability, nonnegative=True)
            if ep > ONE: raise ValueError("Signal entry probability above one")
            object.__setattr__(self, "entry_probability", ep)
        if not all((self.asset, self.idea_id, self.decision_id)) or self.horizon not in HORIZON_SECONDS:
"""
if old not in s: raise SystemExit('DOMAIN_POST_ANCHOR_NOT_FOUND')
p.write_text(s.replace(old,new),encoding='utf-8')

# 2) Routing: invalidated/non-eligible plans cannot create a paper position.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
insert_after="""def fnum(x,default=0.0):
    try:
        if isinstance(x,bool): return default
        y=float(x)
        return y if isfinite(y) else default
    except (ValueError,TypeError): return default


"""
helper="""def entry_probability(row):
    cp=row.get('calibrated_probability')
    if cp is not None:
        try:return max(.50,min(.95,float(cp))),'EMPIRICAL_CALIBRATION'
        except (ValueError,TypeError):pass
    tr=row.get('tactical_reversal') or {}
    if tr.get('active') and tr.get('probability') is not None:
        try:return max(.50,min(.90,float(tr.get('probability')))),'REVERSAL_MODEL_PRIOR_UNCALIBRATED'
        except (ValueError,TypeError):pass
    rs=row.get('range_retest_breakout') or {}
    if rs.get('active') and rs.get('probability') is not None:
        try:return max(.50,min(.90,float(rs.get('probability')))),'RANGE_SETUP_MODEL_PRIOR_UNCALIBRATED'
        except (ValueError,TypeError):pass
    inst=row.get('institutional_signal') or {};bq=inst.get('breakout_quality') or {};ev=inst.get('evidence_independence') or {}
    hs=row.get('horizon_structure') or {};plan=row.get('trade_plan') or {}
    conf=max(0,min(1,fnum(row.get('confidence'))));q=max(0,min(1,fnum(bq.get('quality_score'))))
    indep=max(0,min(1,fnum(ev.get('independent_count'))/6.0));native=max(0,min(1,fnum(hs.get('score'))))
    rr=max(0,min(1,fnum(plan.get('expected_to_stop_ratio'))/3.0))
    prob=.50+.12*conf+.14*q+.08*indep+.08*native+.05*rr
    if str(inst.get('investor_signal') or '').startswith('STRONG'):prob+=.03
    if str(inst.get('investor_signal') or '').startswith('ADD'):prob+=.04
    return max(.50,min(.90,prob)),'MODEL_PRIOR_UNCALIBRATED'


"""
if insert_after not in s: raise SystemExit('ROUTING_HELPER_ANCHOR_NOT_FOUND')
s=s.replace(insert_after,insert_after+helper)
old="""        r,w=ranked[0]; p=r['trade_plan']; h=r['horizon']; fam=family(r)
        desired=max(D('.05'),min(D('1'),decimal(p.get('initial_position_fraction') or '.10',nonnegative=True)))
        if p.get('eligible') is False or (p.get('trade_integrity') or {}).get('entry_permission')=='WAIT_ENTRY': desired=D('.05')
        # Heuristic confidence is not calibration. Larger initial risk requires explicit evidence.
"""
new="""        r,w=ranked[0]; p=r['trade_plan']; h=r['horizon']; fam=family(r)
        if p.get('eligible') is False:
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'BLOCKED','reason':'TRADE_PLAN_NOT_ELIGIBLE'})
            continue
        if (p.get('trade_integrity') or {}).get('entry_permission')=='WAIT_ENTRY':
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'BLOCKED','reason':'ENTRY_PERMISSION_WAIT'})
            continue
        desired=max(D('.05'),min(D('1'),decimal(p.get('initial_position_fraction') or '.10',nonnegative=True)))
        # Heuristic confidence is not calibration. Larger initial risk requires explicit evidence.
"""
if old not in s: raise SystemExit('ROUTING_ADMISSION_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
old="""        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
                          at+timedelta(seconds=lifetime_seconds),fam,str(r.get('regime') or 'UNKNOWN'),
                          confirmations=tuple(unique.values()),validated_add=False)
"""
new="""        prob,prob_source=entry_probability(r)
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
                          at+timedelta(seconds=lifetime_seconds),fam,str(r.get('regime') or 'UNKNOWN'),
                          confirmations=tuple(unique.values()),validated_add=False,
                          entry_probability=D(str(prob)),probability_source=prob_source)
"""
if old not in s: raise SystemExit('ROUTING_SIGNAL_ANCHOR_NOT_FOUND')
p.write_text(s.replace(old,new),encoding='utf-8')

# 3) SQLite paper mode is single-writer in isolated validation service; retain diagnostics.
p=root/'veritas_v85/application.py'
s=p.read_text(encoding='utf-8')
old="""        if self.mode=='paper':
                self.writer_active=self.ledger.acquire_writer()
                if not self.writer_active:return [{'status':'STANDBY','reason':'OTHER_V85_WRITER_OWNS_LEASE','actions':[]}]
"""
new="""        if self.mode=='paper':
                acquire=getattr(self.ledger,'acquire_writer',None)
                self.writer_active=(acquire() if acquire else bool(self.settings.testing))
                if not self.writer_active:return [{'status':'STANDBY','reason':'OTHER_V85_WRITER_OWNS_LEASE','actions':[]}]
"""
if old not in s: raise SystemExit('APP_WRITER_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
old="except Exception as e:self.last_error=name+': '+type(e).__name__;self.ui.failure(self.last_error)"
new="""except Exception as e:
                        import traceback
                        self.last_error=name+': '+type(e).__name__+': '+str(e)[:300]
                        print('V86_WORKER_ERROR '+self.last_error, flush=True)
                        traceback.print_exc()
                        self.ui.failure(self.last_error)"""
if old in s:s=s.replace(old,new)
p.write_text(s,encoding='utf-8')

# 4) Closed episodes persist complete realized exit facts for audit/UI/learning.
p=root/'veritas_v85/book.py'
s=p.read_text(encoding='utf-8')
old="""        outcome={'net_pnl':str(episode_net),'gross_close_leg':str(gross),'funding_close_leg':str(fund),
                 'observed_mfe_fraction':str(mfe),'observed_mae_fraction':str(mae),
"""
new="""        outcome={'net_pnl':str(episode_net),'gross_close_leg':str(gross),'funding_close_leg':str(fund),
                 'entry_fee':str(entry_fee),'exit_fee':str(fee),'exit_price':str(quote.price),
                 'outcome_finalized':True,'finalization_contract':'CLOSED_FINAL_V1',
                 'learning_eligible':bool(len(points)>=2),
                 'observed_mfe_fraction':str(mfe),'observed_mae_fraction':str(mae),
"""
if old not in s: raise SystemExit('BOOK_OUTCOME_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
guard_anchor="        insert_lesson(c,lesson)\n"
guard="""        _required_final=('net_pnl','gross_close_leg','funding_close_leg','entry_fee','exit_fee','exit_price',
                         'observed_mfe_fraction','observed_mae_fraction','giveback_from_observed_peak',
                         'held_seconds','exit_reason','path_points')
        _missing_final=[k for k in _required_final if outcome.get(k) is None]
        if _missing_final:
            raise RuntimeError('CLOSED_FINAL_INCOMPLETE:'+','.join(_missing_final))
        if int(outcome.get('path_points') or 0)>=2:
            insert_lesson(c,lesson)
        else:
            outcome['learning_eligible']=False
            outcome['learning_skip_reason']='INSUFFICIENT_OBSERVED_PATH'
            original['outcome']=outcome
            c.execute('UPDATE v85_episodes SET payload=? WHERE episode_id=?',(canonical_json(original),p.episode_id))
"""
if guard_anchor not in s: raise SystemExit('BOOK_LEARNING_GUARD_ANCHOR_NOT_FOUND')
s=s.replace(guard_anchor,guard,1)
p.write_text(s,encoding='utf-8')

# 5) Routing v86.3: choose the best EXECUTABLE horizon and size in 5% steps up to 250% NAV.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="""        r,w=ranked[0]; p=r['trade_plan']; h=r['horizon']; fam=family(r)
        if p.get('eligible') is False:
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'BLOCKED','reason':'TRADE_PLAN_NOT_ELIGIBLE'})
            continue
        if (p.get('trade_integrity') or {}).get('entry_permission')=='WAIT_ENTRY':
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'BLOCKED','reason':'ENTRY_PERMISSION_WAIT'})
            continue
        desired=max(D('.05'),min(D('1'),decimal(p.get('initial_position_fraction') or '.10',nonnegative=True)))
        # Heuristic confidence is not calibration. Larger initial risk requires explicit evidence.
        if r.get('calibrated_probability') is None: desired=min(desired,D('.20'))
"""
new="""        executable=[]
        for rr,ww in ranked:
            pp=rr.get('trade_plan') or {}
            if pp.get('eligible') is False: continue
            if (pp.get('trade_integrity') or {}).get('entry_permission')=='WAIT_ENTRY': continue
            executable.append((rr,ww))
        if not executable:
            traces.append({'asset':a,'direction':d,'status':'BLOCKED','reason':'NO_EXECUTABLE_HORIZON',
                           'candidate_horizons':[x['horizon'] for x,_ in ranked]})
            continue
        r,w=executable[0]; p=r['trade_plan']; h=r['horizon']; fam=family(r)
        prob,prob_source=entry_probability(r)
        inst=r.get('institutional_signal') or {}
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        rr_ratio=fnum(p.get('expected_to_stop_ratio'),0.0)
        # 5% is the increment, NOT a maximum position size.
        if prob < .60: desired=D('.05')
        elif prob < .65: desired=D('.10')
        elif prob < .70: desired=D('.25')
        elif prob < .75: desired=D('.50')
        elif prob < .80: desired=D('.75')
        elif prob < .85: desired=D('1.00')
        elif prob < .90: desired=D('1.50')
        else: desired=D('2.00')
        # Full 250% authority requires empirically calibrated maximum confidence
        # plus independent evidence and acceptable reward/risk.
        if prob>=.90 and indep>=3 and rr_ratio>=1.50 and prob_source=='EMPIRICAL_CALIBRATION':
            desired=D('2.50')
        desired=max(desired,decimal(p.get('initial_position_fraction') or '.05',nonnegative=True))
        desired=min(D('2.50'),desired)
"""
if old not in s: raise SystemExit('ROUTING_EXECUTABLE_HORIZON_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
old="""        prob,prob_source=entry_probability(r)
        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
new="""        signals[a]=Signal(a,d,idea,did,h,decimal(p['stop_price'],positive=True),desired,at,
"""
if old not in s: raise SystemExit('ROUTING_DUP_PROB_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
p.write_text(s,encoding='utf-8')

# 6) Portfolio risk envelopes: max gross / single-asset capacity 250% in NORMAL state.
p=root/'veritas_v86/portfolios.py'
s=p.read_text(encoding='utf-8')
s=s.replace('max_gross: Decimal = D("2.0")','max_gross: Decimal = D("2.5")')
s=s.replace('asset_cap: Decimal = D("1.0")','asset_cap: Decimal = D("2.5")')
repls={
'max_gross=D("2.0"),stop_risk_nav=D("0.015"),hard_drawdown=D("0.10")':
'max_gross=D("2.5"),asset_cap=D("2.5"),stop_risk_nav=D("0.015"),hard_drawdown=D("0.10")',
'max_gross=D("2.0"),asset_cap=D("0.75"),stop_risk_nav=D("0.020"),hard_drawdown=D("0.12")':
'max_gross=D("2.5"),asset_cap=D("2.5"),stop_risk_nav=D("0.020"),hard_drawdown=D("0.12")',
'max_gross=D("1.5"),asset_cap=D("0.60"),stop_risk_nav=D("0.0125"),hard_drawdown=D("0.10")':
'max_gross=D("2.5"),asset_cap=D("2.5"),stop_risk_nav=D("0.0125"),hard_drawdown=D("0.10")',
'max_gross=D("1.5"),asset_cap=D("0.60"),stop_risk_nav=D("0.015"),hard_drawdown=D("0.10")':
'max_gross=D("2.5"),asset_cap=D("2.5"),stop_risk_nav=D("0.015"),hard_drawdown=D("0.10")',
'max_gross=D("1.5"),asset_cap=D("0.50"),stop_risk_nav=D("0.020"),hard_drawdown=D("0.12")':
'max_gross=D("2.5"),asset_cap=D("2.5"),stop_risk_nav=D("0.020"),hard_drawdown=D("0.12")'
}
for a,b in repls.items(): s=s.replace(a,b)
p.write_text(s,encoding='utf-8')

# 7) Observable signal -> execution contract.
p=root/'veritas_v86/application.py'
s=p.read_text(encoding='utf-8')
old="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
        if self.mode!='audit' and self.last_routes is not None:
"""
new="""    def commit_cycle(self,summary,bundles,cycle_id,clock_ok=True):
        out=super().commit_cycle(summary,bundles,cycle_id,clock_ok=clock_ok)
        try:
            actions=[]
            for pf in out.get('portfolios') or []:
                for a in pf.get('actions') or []:
                    actions.append({'portfolio':pf.get('name'),'asset':a.get('asset'),
                                    'reason':a.get('management_reason'),'events':a.get('events'),
                                    'target_fraction':a.get('target_fraction'),
                                    'current_fraction':a.get('current_fraction'),
                                    'risk':a.get('risk') or a.get('risk_state'),
                                    'signal_present':a.get('signal_present')})
            print(json.dumps({'event':'V86_EXECUTION_TRACE','routing':out.get('routing') or [],
                              'quote_errors':out.get('quote_errors') or [],
                              'quote_keys':sorted(self.quotes.keys()),
                              'actions':actions},ensure_ascii=False,separators=(',',':')),flush=True)
        except Exception as exc:
            print(json.dumps({'event':'V86_EXECUTION_TRACE_ERROR','error':type(exc).__name__},
                             separators=(',',':')),flush=True)
        if self.mode!='audit' and self.last_routes is not None:
"""
if old not in s: raise SystemExit('V86_EXECUTION_TRACE_ANCHOR_NOT_FOUND')
p.write_text(s.replace(old,new),encoding='utf-8')

# 8) Replace cash NDX with nearly-24h CME E-mini Nasdaq-100 futures (NQ).
# Rename the asset across runtime code/knowledge while keeping old NDX episodes only in archive services.
for ext in ('*.py','*.json'):
    for fp in root.rglob(ext):
        try:
            txt=fp.read_text(encoding='utf-8')
        except Exception:
            continue
        if 'NDX' not in txt and '%5ENQ' not in txt and '^NQ' not in txt:
            continue
        txt=txt.replace('NDX','NQ')
        txt=txt.replace('%5ENQ','NQ%3DF').replace('^NQ','NQ%3DF')
        fp.write_text(txt,encoding='utf-8')

# Main model: use NQ/MNQ futures bars and a futures-session freshness gate instead of US cash RTH.
p=root/'veritas_intelligence.py'
s=p.read_text(encoding='utf-8')
import re
s=s.replace("'NQ':   {'1h':1,'4h':4,'1d':7,'3d':20,'7d':46}",
            "'NQ':   {'1h':1,'4h':4,'1d':23,'3d':69,'7d':161}")
s=s.replace("if asset=='NQ':\n        raw=_ndx_market(); deriv=_ndx_derivatives_context()",
            "if asset=='NQ':\n        raw=_ndx_market(); deriv=_research_only_derivatives(asset)")
old_exec="""    if asset=='NQ':
        # _ndx_market's source gate already requires current Nasdaq-100 quote + public cross-check.
        return {'eligible':bool(research_ok and time_ok),'reason':'two_direct_index_checks' if research_ok else 'ndx_verification_failed',
                'direct_sources':2 if research_ok else 1,'research_ok':research_ok,'time_ok':time_ok}
"""
new_exec="""    if asset=='NQ':
        # Paper execution uses the E-mini NQ quote and Micro E-mini MNQ paired-contract check.
        # Same underlying/exchange; this is NOT claimed as two independent data vendors.
        paired=(raw.get('verification_mode')=='paired_contract_same_underlying')
        ok=bool(research_ok and time_ok and paired and float(raw.get('source_divergence') or 0)<=0.0025)
        return {'eligible':ok,
                'reason':'nq_paired_contract_paper_gate' if ok else 'nq_futures_verification_failed',
                'direct_sources':1,'direct_contracts':2 if paired else 1,
                'independent_vendors':1,'paper_only':True,
                'research_ok':research_ok,'time_ok':time_ok}
"""
if old_exec not in s:
    raise SystemExit('NQ_EXECUTION_GATE_ANCHOR_NOT_FOUND')
s=s.replace(old_exec,new_exec)

pattern=r"def _ndx_market\(\):\n.*?\n\ndef _research_only_derivatives"
new_market="""def _ndx_market():
    # NQ replaces cash NDX: E-mini Nasdaq-100 futures trade nearly around the clock.
    nq5,_=_yahoo_series('NQ%3DF','5d','5m',True)
    nq1h,_=_yahoo_series('NQ%3DF','3mo','1h',True)
    mnq5,_=_yahoo_series('MNQ%3DF','5d','5m',True)
    try:
        nqdaily,_=_yahoo_series('NQ%3DF','1y','1d',True)
    except Exception:
        nqdaily=[]
    if len(nq1h)<200:
        raise RuntimeError(f'INSUFFICIENT_NQ_HOURLY_BARS {len(nq1h)}')
    if not nq5:
        raise RuntimeError('NQ_5M_UNAVAILABLE')
    pbar=nq5[-1]; price=float(pbar['close'])
    pts=datetime.fromtimestamp(pbar['ts'],tz=timezone.utc).isoformat()
    sec=None; sec_ts=None
    if mnq5:
        sbar=mnq5[-1]; sec=float(sbar['close'])
        sec_ts=datetime.fromtimestamp(sbar['ts'],tz=timezone.utc).isoformat()
    mid=((price+sec)/2) if sec is not None else None
    div=(abs(price-sec)/mid) if mid else 999.0
    w=nq1h[-360:]
    closes=[float(x['close']) for x in w]
    highs=[float(x['high']) for x in w]
    lows=[float(x['low']) for x in w]
    vols=[float(x.get('volume') or 0) for x in w]
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    age=_age_seconds(pts); sec_age=_age_seconds(sec_ts) if sec_ts else None
    market_open=_futures_market_open_from_age(pts)
    gate=bool(market_open and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS
              and sec is not None and sec_age is not None and sec_age<=DELAYED_FUTURES_MAX_AGE_SECONDS
              and div<=0.0025)
    quality=[
      _source_row('Yahoo CME NQ=F','E-mini Nasdaq-100 futures','primary delayed futures',pts,600,
                  'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',
                  'Yahoo CME delayed feed; paper testing only','Yahoo/CME'),
      _source_row('Yahoo CME MNQ=F','Micro E-mini Nasdaq-100 futures','paired-contract verification',sec_ts,600,
                  'PAIRED_OK' if gate else 'PAIRED_FAIL',
                  f'same underlying/exchange; divergence={div:.4%}; not an independent vendor','Yahoo/CME')
    ]
    _set_source_quality(quality)
    quote_event='NQ:'+str(int(pbar['ts']))+':'+format(price,'.8f')+':'+(format(sec,'.8f') if sec is not None else 'NA')
    return {'asset':'NQ','price':price,'secondary_price':sec,'coinbase_price':sec,
            'secondary_observed_at':sec_ts,'source_divergence':div,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(pbar['ts']*1000),'observed_at':pts,
            'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
            'intraday_bars':nq5,'daily_bars':nqdaily,'volume_intraday_bars':nq5,
            'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'paired_contract_same_underlying',
            'source_names':{'primary':'Yahoo CME NQ=F','secondary':'Yahoo CME MNQ=F'},
            'v85_quote':{
                'event_id':quote_event,
                'primary_time':pts,
                'secondary_time':sec_ts,
                'primary_source':'Yahoo CME NQ=F',
                'secondary_source':'Yahoo CME MNQ=F',
                'source_verified':bool(gate),
                'max_age_seconds':float(DELAYED_FUTURES_MAX_AGE_SECONDS)
            },
            'contract':{'symbol':'NQ','underlying':'Nasdaq-100','exchange':'CME',
                        'multiplier_usd_per_point':20.0,'tick_points':0.25,'tick_value_usd':5.0,
                        'currency':'USD','execution':'synthetic_paper'}}


def _research_only_derivatives"""
s2,n=re.subn(pattern,new_market,s,count=1,flags=re.S)
if n!=1:
    raise SystemExit(f'NQ_MARKET_REPLACE_FAILED {n}')
p.write_text(s2,encoding='utf-8')

print('V86_NDX_TO_NQ_PATCH_OK')
# 9) Signal-to-trade bridge: a directional signal on ANY horizon opens a small probe
# when data/source gates pass and there is no HARD veto. Soft timing conflicts no longer
# erase the trade; they reduce initial size. Confirmation later raises target in 5% steps.
p=root/'veritas_v85/routing.py'
s=p.read_text(encoding='utf-8')
old="""        executable=[]
        for rr,ww in ranked:
            pp=rr.get('trade_plan') or {}
            if pp.get('eligible') is False: continue
            if (pp.get('trade_integrity') or {}).get('entry_permission')=='WAIT_ENTRY': continue
            executable.append((rr,ww))
        if not executable:
            traces.append({'asset':a,'direction':d,'status':'BLOCKED','reason':'NO_EXECUTABLE_HORIZON',
                           'candidate_horizons':[x['horizon'] for x,_ in ranked]})
            continue
        r,w=executable[0]; p=r['trade_plan']; h=r['horizon']; fam=family(r)
        prob,prob_source=entry_probability(r)
        inst=r.get('institutional_signal') or {}
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        rr_ratio=fnum(p.get('expected_to_stop_ratio'),0.0)
        # 5% is the increment, NOT a maximum position size.
        if prob < .60: desired=D('.05')
        elif prob < .65: desired=D('.10')
        elif prob < .70: desired=D('.25')
        elif prob < .75: desired=D('.50')
        elif prob < .80: desired=D('.75')
        elif prob < .85: desired=D('1.00')
        elif prob < .90: desired=D('1.50')
        else: desired=D('2.00')
        # Full 250% authority requires empirically calibrated maximum confidence
        # plus independent evidence and acceptable reward/risk.
        if prob>=.90 and indep>=3 and rr_ratio>=1.50 and prob_source=='EMPIRICAL_CALIBRATION':
            desired=D('2.50')
        desired=max(desired,decimal(p.get('initial_position_fraction') or '.05',nonnegative=True))
        desired=min(D('2.50'),desired)
"""
new="""        executable=[]; probeable=[]
        for rr,ww in ranked:
            pp=rr.get('trade_plan') or {}
            ti=pp.get('trade_integrity') or rr.get('trade_integrity') or {}
            arb=pp.get('rule_arbitration') or rr.get('rule_arbitration') or {}
            hard=bool(ti.get('hard_invalidation') or arb.get('hard_veto'))
            if hard:
                continue
            stop=fnum(pp.get('stop_price'),0.0)
            price=fnum(rr.get('price'),0.0)
            if stop<=0 or price<=0:
                rs=rr.get('range_retest_breakout') or {}
                tr=rr.get('tactical_reversal') or {}
                cand=rs if rs.get('candidate_direction')==d else tr if tr.get('candidate_direction')==d else {}
                stop=fnum(cand.get('stop_price'),0.0)
                if stop>0: pp=dict(pp,stop_price=stop)
            stop_ok=bool(stop>0 and price>0 and ((d=='LONG' and stop<price) or (d=='SHORT' and stop>price)))
            if not stop_ok:
                continue
            if pp.get('eligible') is not False and ti.get('entry_permission')!='WAIT_ENTRY':
                executable.append((rr,ww,pp))
                continue
            exec_tier=str(rr.get('execution_signal_tier') or rr.get('signal_tier') or '')
            directional=rr.get('research_decision')==d and exec_tier in (d,'SUPER_'+d)
            if directional and rr.get('execution_eligible',True):
                probeable.append((rr,ww,pp))
        probe_mode=False
        if executable:
            r,w,p=executable[0]
        elif probeable:
            r,w,p=probeable[0]; probe_mode=True
        else:
            traces.append({'asset':a,'direction':d,'status':'BLOCKED','reason':'NO_SAFE_ENTRY_HORIZON',
                           'candidate_horizons':[x['horizon'] for x,_ in ranked]})
            continue
        h=r['horizon']; fam=family(r)
        prob,prob_source=entry_probability(r)
        inst=r.get('institutional_signal') or {}
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
        rr_ratio=fnum(p.get('expected_to_stop_ratio'),0.0)

        if probe_mode:
            # A valid directional signal must reach the book. Soft timing/invalidation
            # reduces exposure; it no longer silently converts the signal to cash.
            if prob < .65: desired=D('.05')
            elif prob < .70: desired=D('.10')
            elif prob < .75: desired=D('.15')
            else: desired=D('.25')
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'ROUTED_PROBE',
                           'reason':'SOFT_CONFLICT_EARLY_ENTRY','target_fraction':str(desired),
                           'probability':round(prob,4),'probability_source':prob_source})
        else:
            # Confirmed sizing. All targets are multiples of the 5% position step.
            if prob < .60: desired=D('.05')
            elif prob < .65: desired=D('.10')
            elif prob < .70: desired=D('.25')
            elif prob < .75: desired=D('.50')
            elif prob < .80: desired=D('.75')
            elif prob < .85: desired=D('1.00')
            elif prob < .90: desired=D('1.50')
            else: desired=D('2.00')
            if prob>=.90 and indep>=3 and rr_ratio>=1.50 and prob_source=='EMPIRICAL_CALIBRATION':
                desired=D('2.50')
            desired=max(desired,decimal(p.get('initial_position_fraction') or '.05',nonnegative=True))
            desired=min(D('2.50'),desired)
            traces.append({'asset':a,'direction':d,'horizon':h,'status':'ROUTED_CONFIRMED',
                           'target_fraction':str(desired),'probability':round(prob,4),
                           'probability_source':prob_source})
"""
if old not in s: raise SystemExit('SIGNAL_TO_TRADE_BRIDGE_ANCHOR_NOT_FOUND')
s=s.replace(old,new)
p.write_text(s,encoding='utf-8')

print('V86_SIGNAL_TO_TRADE_BRIDGE_OK')
# 10) Execution eligibility is a hard source/data gate before any trade candidate is created.
p=root/'veritas_v85/routing.py'
_s=p.read_text(encoding='utf-8')
_old="""        reason='CONFLICTING_DUPLICATE_CELL' if (a,h) in conflicts else hard_reason(r)
        if reason:
"""
_new="""        reason='CONFLICTING_DUPLICATE_CELL' if (a,h) in conflicts else hard_reason(r)
        if not reason and r.get('execution_eligible') is False:
            reason='EXECUTION_SOURCE_GATE:'+str(r.get('execution_reason') or 'not_eligible')
        if reason:
"""
if _old not in _s: raise SystemExit('EXECUTION_SOURCE_GATE_ANCHOR_NOT_FOUND')
p.write_text(_s.replace(_old,_new),encoding='utf-8')

# TEMP diagnostic: expose v86 mandate filters during build so hidden signal drops are auditable.
try:
    _pp=(root/'veritas_v86/portfolios.py').read_text(encoding='utf-8').splitlines()
    _keys=('MANDATE','mandate','accept','eligible','signal','setup','asset','Champion','Challenger','Impulse','Trend','Range','Reversal','Event','RelativeValue')
    _hits=[i for i,line in enumerate(_pp) if any(k in line for k in _keys)]
    _seen=set()
    for i in _hits:
        a=max(0,i-2); b=min(len(_pp),i+4); key=(a,b)
        if key in _seen: continue
        _seen.add(key)
        print('V86_PORTFOLIOS_SRC '+str(a+1)+'-'+str(b)+' :: '+' | '.join(_pp[a:b]),flush=True)
except Exception as _ex:
    print('V86_PORTFOLIOS_SRC_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# TEMP diagnostic for v86 application signal handoff.
try:
    _ap=(root/'veritas_v86/application.py').read_text(encoding='utf-8').splitlines()
    _keys=('last_routes','signal_allowed','signals','quotes','portfolio','book','asset','commit_cycle','step(')
    _hits=[i for i,line in enumerate(_ap) if any(k in line for k in _keys)]
    _seen=set()
    for i in _hits:
        a=max(0,i-3); b=min(len(_ap),i+5); key=(a,b)
        if key in _seen: continue
        _seen.add(key)
        print('V86_APP_SRC '+str(a+1)+'-'+str(b)+' :: '+' | '.join(_ap[a:b]),flush=True)
except Exception as _ex:
    print('V86_APP_SRC_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# TEMP diagnostic for PaperBook signal admission.
try:
    _bk=(root/'veritas_v85/book.py').read_text(encoding='utf-8').splitlines()
    _keys=('def process','NO_SIGNAL','SIGNAL_FIRST_RISK_VALIDATED','signal is None','desired_fraction','quantity','lot','min_qty','RiskLimits','new_risk_allowed','asset_cap','gross_cap','stop_risk')
    _hits=[i for i,line in enumerate(_bk) if any(k in line for k in _keys)]
    _seen=set()
    for i in _hits:
        a=max(0,i-4); b=min(len(_bk),i+7); key=(a,b)
        if key in _seen: continue
        _seen.add(key)
        print('V86_BOOK_SRC '+str(a+1)+'-'+str(b)+' :: '+' | '.join(_bk[a:b]),flush=True)
except Exception as _ex:
    print('V86_BOOK_SRC_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# TEMP exact PaperBook admission slice.
try:
    _bk2=(root/'veritas_v85/book.py').read_text(encoding='utf-8').splitlines()
    print('V86_BOOK_EXACT_228_260 :: '+' | '.join(_bk2[227:260]),flush=True)
except Exception as _ex:
    print('V86_BOOK_EXACT_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# TEMP exact diagnostics for quote construction / instrument registry.
try:
    _app=(root/'veritas_v85/application.py').read_text(encoding='utf-8').splitlines()
    for _a,_b in ((1,80),(80,170),(170,280)):
        _chunk=_app[_a-1:_b]
        if any(('Quote(' in x or 'quotes' in x or 'commit_cycle' in x or 'bundles' in x or 'self.quotes' in x) for x in _chunk):
            print('V86_V85APP_EXACT '+str(_a)+'-'+str(_b)+' :: '+' | '.join(_chunk),flush=True)
    _dom=(root/'veritas_v85/domain.py').read_text(encoding='utf-8').splitlines()
    for _i,_line in enumerate(_dom):
        if 'class Quote' in _line or '@dataclass' in _line and _i+1<len(_dom) and 'Quote' in _dom[_i+1]:
            print('V86_QUOTE_DEF :: '+' | '.join(_dom[max(0,_i-2):min(len(_dom),_i+28)]),flush=True)
    _cfg=(root/'veritas_v85/config.py').read_text(encoding='utf-8').splitlines()
    for _i,_line in enumerate(_cfg):
        if 'def instruments' in _line or 'InstrumentSpec' in _line or 'NQ' in _line:
            print('V86_CONFIG_SRC '+str(_i+1)+' :: '+' | '.join(_cfg[max(0,_i-2):min(len(_cfg),_i+10)]),flush=True)
except Exception as _ex:
    print('V86_QUOTE_DIAG_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# TEMP exact routing quote/source-gate diagnostics.
try:
    _rt=(root/'veritas_v85/routing.py').read_text(encoding='utf-8').splitlines()
    for _i,_line in enumerate(_rt):
        if _line.startswith('def quote_from_raw') or _line.startswith('def route'):
            print('V86_ROUTING_EXACT '+str(_i+1)+' :: '+' | '.join(_rt[_i:min(len(_rt),_i+85)]),flush=True)
except Exception as _ex:
    print('V86_ROUTING_EXACT_ERROR '+type(_ex).__name__+': '+str(_ex)[:200],flush=True)
# 11) Persistence guard: live paper testing must never silently fall back to ephemeral /tmp SQLite.
p=root/'veritas_v86_start.py'
_start=p.read_text(encoding='utf-8')
_guard="""import os as _veritas_os
_v86_mode=_veritas_os.getenv('VERITAS_V85_MODE','audit').lower()
_v86_persist_required=_veritas_os.getenv('VERITAS_V86_PERSISTENCE_REQUIRED','1')=='1'
_v86_db_url=_veritas_os.getenv('VERITAS_V85_TEST_DATABASE_URL') or _veritas_os.getenv('DATABASE_URL')
_v86_sqlite=_veritas_os.getenv('VERITAS_V85_TEST_SQLITE','')
_v86_ephemeral=_veritas_os.getenv('VERITAS_V85_EPHEMERAL_DB','0')=='1'
if _v86_mode=='paper' and _v86_persist_required:
    if not _v86_db_url and (_v86_sqlite.startswith('/tmp/') or not _v86_sqlite):
        raise RuntimeError('PERSISTENCE_REQUIRED: paper engine refuses ephemeral /tmp state')
    if _v86_ephemeral and _veritas_os.getenv('VERITAS_V86_ALLOW_EPHEMERAL_STATE','0')!='1':
        raise RuntimeError('PERSISTENCE_REQUIRED: VERITAS_V85_EPHEMERAL_DB is forbidden for cumulative paper testing')
"""
if 'PERSISTENCE_REQUIRED: paper engine refuses ephemeral /tmp state' not in _start:
    p.write_text(_guard+'\n'+_start,encoding='utf-8')

# Test/cohort timestamps are labels only; they may not imply a new ledger or reset.
p=root/'veritas_v86/state_policy.json'
p.write_text(json.dumps({
  'policy_version':'v86-cumulative-1',
  'reset_allowed':False,
  'initial_nav_rub_per_portfolio':1000000,
  'position_step':0.05,
  'max_position_fraction':2.50,
  'storage':'durable_postgres_required',
  'preserve':['accounts','positions','episodes','orders','closed_trades','nav_history',
              'decisions','outcomes','learning_events','calibration','adaptive_evidence',
              'performance_gates','opportunities'],
  'deployment_rule':'schema migrations must be additive/idempotent; no DROP/TRUNCATE/reset in normal deploy',
  'learning_rule':'closed episodes and missed-opportunity episodes accumulate across versions/cohorts'
},ensure_ascii=False,indent=2)+'\n',encoding='utf-8')

print('V86_STAGED_EXECUTION_SOURCES_SKIPPED_IN_RUNTIME')


# 12) Restore pre-durable state once, without fabricating closes/P&L/lessons.
_restore_target=root.parent/'state_snapshots/VERITAS_v86_STATE_20260924_111755Z.json'
_restore_archive=root.parent/'state_snapshots/VERITAS_v86_STATE_20260924_093520Z.json'
if not _restore_target.is_file() or not _restore_archive.is_file():
    raise SystemExit('V86_STATE_RESTORE_SNAPSHOT_MISSING')
(root/'veritas_v86/state_restore.py').write_text("\"\"\"One-time, fail-closed continuity restore from the last verified pre-durable snapshot.\nFinancial continuity only: no synthetic close, no fabricated P&L, no learning lesson.\n\"\"\"\nfrom __future__ import annotations\nfrom pathlib import Path\nfrom decimal import Decimal\nfrom datetime import datetime, timezone\nimport hashlib, json\n\nRESTORE_ID='PRE_DURABLE_STATE_20260924_111755Z'\nHERE=Path(__file__).resolve().parent\nTARGET=HERE/'state_restore_111755.json'\nARCHIVE=HERE/'state_archive_093520.json'\n\ndef _obj(x):\n    if isinstance(x,dict): return dict(x)\n    if isinstance(x,str):\n        try:\n            y=json.loads(x)\n            return y if isinstance(y,dict) else {}\n        except Exception:\n            return {}\n    return {}\n\ndef _load(path):\n    return json.loads(path.read_text(encoding='utf-8'))\n\ndef _intent(ep):\n    return 'RESTORE_'+hashlib.sha256((RESTORE_ID+':'+ep).encode()).hexdigest()[:40]\n\ndef _mark_event(ep):\n    return 'RESTORE_MARK_'+hashlib.sha256((RESTORE_ID+':MARK:'+ep).encode()).hexdigest()[:32]\n\ndef _rowdict(r):\n    return dict(r) if r is not None else None\n\ndef restore_pre_durable_state(ledger, *, allow_non_postgres=False):\n    if getattr(ledger,'dialect',None)!='postgres' and not allow_non_postgres:\n        return {'status':'SKIPPED_NON_POSTGRES','restore_id':RESTORE_ID}\n    target=_load(TARGET); archive=_load(ARCHIVE)\n    target_marks={}\n    for pf in target.get('portfolios') or []:\n        for p in pf.get('positions') or []:\n            target_marks[(str(pf.get('name')),str(p.get('asset')))]=p\n    target_trades=[x for x in (target.get('trades') or []) if str(x.get('status') or '').upper()=='OPEN']\n    applied_at=datetime.now(timezone.utc).isoformat()\n    with ledger.transaction() as c:\n        c.execute(\"\"\"CREATE TABLE IF NOT EXISTS v86_state_restore_audit(\n          restore_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, source_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)\"\"\")\n        c.execute(\"\"\"CREATE TABLE IF NOT EXISTS v86_state_archive(\n          snapshot_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)\"\"\")\n        for snap,reason in ((archive,'INTERRUPTED_PREVIOUS_EPHEMERAL_STATE'),\n                            (target,'PRE_DURABLE_CONTINUITY_SNAPSHOT')):\n            c.execute(\"\"\"INSERT INTO v86_state_archive(snapshot_id,captured_at,reason,payload)\n                         VALUES(?,?,?,?) ON CONFLICT(snapshot_id) DO NOTHING\"\"\",\n                      (str(snap.get('snapshot_id')),str(snap.get('captured_at')),reason,\n                       json.dumps(snap,ensure_ascii=False,separators=(',',':'))))\n        old=c.execute('SELECT payload FROM v86_state_restore_audit WHERE restore_id=?',(RESTORE_ID,)).fetchone()\n        if old:\n            payload=_obj(old['payload'])\n            return {'status':'ALREADY_APPLIED','restore_id':RESTORE_ID,**payload}\n\n        current={}\n        rows=c.execute(\"\"\"SELECT p.account_id,p.asset,p.episode_id,p.payload AS position_payload,p.entry_fee,\n                                p.last_price,p.last_fx,e.idea_id,e.opened_at,e.status,e.payload AS episode_payload\n                         FROM v85_positions p JOIN v85_episodes e ON e.episode_id=p.episode_id\"\"\").fetchall()\n        for row in rows:\n            d=_rowdict(row);current[(str(d['account_id']),str(d['asset']))]=d\n\n        replaced=[];skipped=[];fee_adjustment={}\n        for tr in target_trades:\n            account=str(tr.get('account_id')); asset=str(tr.get('asset')); key=(account,asset)\n            old_payload=_obj(tr.get('payload')); old_pos=_obj(old_payload.get('position'))\n            cur=current.get(key)\n            if cur is None:\n                skipped.append({'account':account,'asset':asset,'reason':'NO_CURRENT_POSITION_TO_REPLACE'})\n                continue\n            cur_payload=_obj(cur.get('episode_payload')); cur_pos=_obj(cur.get('position_payload'))\n            same_context=(str(cur.get('idea_id'))==str(tr.get('idea_id')) and\n                          str(cur_pos.get('direction'))==str(old_pos.get('direction')) and\n                          str(cur_pos.get('horizon'))==str(old_pos.get('horizon')))\n            if not same_context:\n                skipped.append({'account':account,'asset':asset,'reason':'CURRENT_CONTEXT_CHANGED'})\n                continue\n            if str(cur.get('episode_id'))==str(tr.get('episode_id')):\n                skipped.append({'account':account,'asset':asset,'reason':'ALREADY_TARGET_EPISODE'})\n                continue\n\n            old_ep=str(tr['episode_id']); cur_ep=str(cur['episode_id'])\n            existing=c.execute('SELECT status,payload FROM v85_episodes WHERE episode_id=?',(old_ep,)).fetchone()\n            if existing and str(existing['status'])!='OPEN':\n                skipped.append({'account':account,'asset':asset,'reason':'TARGET_EPISODE_EXISTS_NONOPEN'})\n                continue\n\n            cur_payload['state_restore']={'restore_id':RESTORE_ID,'status':'REPLACED_BY_PRE_DURABLE_EPISODE',\n                                          'source_snapshot_id':target.get('snapshot_id'),'replacement_episode_id':old_ep}\n            c.execute(\"UPDATE v85_episodes SET status='MIGRATION_DUPLICATE',payload=? WHERE episode_id=?\",\n                      (json.dumps(cur_payload,ensure_ascii=False,separators=(',',':')),cur_ep))\n            c.execute('DELETE FROM v85_pending_exits WHERE episode_id=?',(cur_ep,))\n            c.execute('DELETE FROM v85_positions WHERE account_id=? AND asset=?',(account,asset))\n\n            old_payload['state_restore']={'restore_id':RESTORE_ID,'status':'RESTORED_ACTIVE_CONTINUATION',\n                                          'source_snapshot_id':target.get('snapshot_id'),\n                                          'source_captured_at':target.get('captured_at'),\n                                          'replaced_episode_id':cur_ep}\n            old_payload_text=json.dumps(old_payload,ensure_ascii=False,separators=(',',':'))\n            if not existing:\n                c.execute(\"\"\"INSERT INTO v85_episodes\n                  (episode_id,account_id,asset,idea_id,policy_version,opened_at,closed_at,opening_event,closing_event,status,payload,net_pnl)\n                  VALUES(?,?,?,?,?,?,NULL,?,NULL,'OPEN',?,NULL)\"\"\",\n                  (old_ep,account,asset,str(tr.get('idea_id')),str(tr.get('policy_version')),\n                   str(tr.get('opened_at')),str(tr.get('opening_event')),old_payload_text))\n            else:\n                c.execute(\"UPDATE v85_episodes SET status='OPEN',payload=? WHERE episode_id=?\",(old_payload_text,old_ep))\n\n            markrow=target_marks.get(key) or {}\n            mark=str(markrow.get('mark') or markrow.get('last_price') or old_pos.get('entry_price'))\n            old_fee=Decimal(str(old_payload.get('entry_fee') or '0'))\n            cur_fee=Decimal(str(cur.get('entry_fee') or '0'))\n            c.execute(\"\"\"INSERT INTO v85_positions(account_id,asset,episode_id,payload,entry_fee,last_price,last_fx)\n                         VALUES(?,?,?,?,?,?,?)\"\"\",\n                      (account,asset,old_ep,json.dumps(old_pos,ensure_ascii=False,separators=(',',':')),\n                       str(old_fee),mark,'1'))\n\n            acc=c.execute('SELECT realized_equity FROM v85_accounts WHERE account_id=?',(account,)).fetchone()\n            if acc:\n                revised=Decimal(str(acc['realized_equity']))+cur_fee-old_fee\n                c.execute('UPDATE v85_accounts SET realized_equity=? WHERE account_id=?',(str(revised),account))\n                fee_adjustment[account]=str(Decimal(fee_adjustment.get(account,'0'))+cur_fee-old_fee)\n\n            side='BUY' if str(old_pos.get('direction'))=='LONG' else 'SHORT'\n            c.execute(\"\"\"INSERT INTO v85_orders(intent_id,episode_id,account_id,event_id,side,quantity,price,fee,reason,at)\n                         VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(intent_id) DO NOTHING\"\"\",\n                      (_intent(old_ep),old_ep,account,str(tr.get('opening_event')),side,str(old_pos.get('quantity')),\n                       str(old_pos.get('entry_price')),str(old_fee),'STATE_SNAPSHOT_RESTORE_OBSERVED_OPEN',str(tr.get('opened_at'))))\n            c.execute(\"\"\"INSERT INTO v85_path(episode_id,event_id,at,price) VALUES(?,?,?,?)\n                         ON CONFLICT(episode_id,event_id) DO NOTHING\"\"\",\n                      (old_ep,str(tr.get('opening_event')),str(tr.get('opened_at')),str(old_pos.get('entry_price'))))\n            c.execute(\"\"\"INSERT INTO v85_path(episode_id,event_id,at,price) VALUES(?,?,?,?)\n                         ON CONFLICT(episode_id,event_id) DO NOTHING\"\"\",\n                      (old_ep,_mark_event(old_ep),str(target.get('captured_at')),mark))\n            replaced.append({'account':account,'asset':asset,'restored_episode_id':old_ep,'replaced_episode_id':cur_ep})\n\n        result={'replaced_count':len(replaced),'skipped_count':len(skipped),'replaced':replaced,'skipped':skipped,\n                'fee_adjustment':fee_adjustment,'target_snapshot_id':target.get('snapshot_id'),\n                'archived_snapshot_id':archive.get('snapshot_id'),\n                'learning_created':False,'synthetic_close_created':False}\n        c.execute('INSERT INTO v86_state_restore_audit VALUES(?,?,?,?)',\n                  (RESTORE_ID,applied_at,str(target.get('snapshot_id')),\n                   json.dumps(result,ensure_ascii=False,separators=(',',':'))))\n    return {'status':'APPLIED','restore_id':RESTORE_ID,**result}\n",encoding='utf-8')
(root/'veritas_v86/state_restore_111755.json').write_text(_restore_target.read_text(encoding='utf-8'),encoding='utf-8')
(root/'veritas_v86/state_archive_093520.json').write_text(_restore_archive.read_text(encoding='utf-8'),encoding='utf-8')

p=root/'veritas_v86/application.py'
_s=p.read_text(encoding='utf-8')
_old="""    app=Application(model,ledger,settings,fixture_bundles=fixture_bundles);app.bootstrap()
    server=ThreadingHTTPServer(('0.0.0.0',settings.port),handler(app));server.daemon_threads=True
"""
_new="""    app=Application(model,ledger,settings,fixture_bundles=fixture_bundles)
    if getattr(ledger,'dialect',None)=='postgres':
        model.pg_init()
        from .state_restore import restore_pre_durable_state
        restore_result=restore_pre_durable_state(ledger)
        print(canonical_json({'event':'V86_STATE_RESTORE',**restore_result}),flush=True)
    app.bootstrap()
    server=ThreadingHTTPServer(('0.0.0.0',settings.port),handler(app));server.daemon_threads=True
"""
if _old not in _s: raise SystemExit('V86_RUN_RESTORE_ANCHOR_NOT_FOUND')
p.write_text(_s.replace(_old,_new,1),encoding='utf-8')
print('V86_STATE_RESTORE_PATCH_OK')


# 13) Paper mode must use DATABASE_URL even if a stale test SQLite env var still exists.
(root/'veritas_v86/state_restore.py').write_text("\"\"\"Durable-state continuity restore for VERITAS v86.\n\nRestores only facts captured in verified snapshots. It never fabricates a close,\nrealized P&L, or a learning lesson. Interrupted pre-durable episodes remain audit-only.\n\"\"\"\nfrom __future__ import annotations\nfrom pathlib import Path\nfrom decimal import Decimal\nfrom datetime import datetime, timezone\nimport hashlib, json\n\nRESTORE_ID='PRE_DURABLE_STATE_20260924_111755Z'\nCUTOVER_ID='legacy-paper-to-v85-v1'\nHERE=Path(__file__).resolve().parent\nTARGET=HERE/'state_restore_111755.json'\nARCHIVE=HERE/'state_archive_093520.json'\n\ndef _obj(x):\n    if isinstance(x,dict): return dict(x)\n    if isinstance(x,str):\n        try:\n            y=json.loads(x)\n            return y if isinstance(y,dict) else {}\n        except Exception:\n            return {}\n    return {}\n\ndef _load(path): return json.loads(path.read_text(encoding='utf-8'))\n\ndef _intent(ep):\n    return 'RESTORE_'+hashlib.sha256((RESTORE_ID+':'+ep).encode()).hexdigest()[:40]\n\ndef _mark_event(ep):\n    return 'RESTORE_MARK_'+hashlib.sha256((RESTORE_ID+':MARK:'+ep).encode()).hexdigest()[:32]\n\ndef _rowdict(r): return dict(r) if r is not None else None\n\ndef _target_accounts(target):\n    out={}\n    for pf in target.get('portfolios') or []:\n        name=str(pf.get('name'))\n        if not name: continue\n        initial=str(pf.get('initial_equity') or '1000000')\n        realized=str(pf.get('realized_equity') or initial)\n        policy='86.0.0-dev1/'+name\n        positions=pf.get('positions') or []\n        if positions:\n            policy=str(positions[0].get('policy_version') or policy)\n        out[name]={'account_id':name,'initial_equity':initial,'realized_equity':realized,\n                   'policy_version':policy,'source':{'last_ruonia':'0',\n                   'state_snapshot_id':target.get('snapshot_id')}}\n    return out\n\ndef _cutover_payload(target, accounts):\n    snap={'accounts':list(accounts.values()),'positions':[]}\n    return {'status':'OK','cutover_id':CUTOVER_ID,'as_of':str(target.get('captured_at')),\n            'price_model':'NORMALIZED_REFERENCE_LEVEL_RUB',\n            'source_hash':'STATE_SNAPSHOT:'+str(target.get('snapshot_id')),\n            'snapshot':snap,'account_count':len(accounts),\n            'position_count':int(target.get('trade_count') or 0),\n            'legacy_tables_untouched':True,\n            'restore_policy':'VERIFIED_STATE_SNAPSHOT_NO_SYNTHETIC_CLOSE'}\n\ndef _insert_target_episode(c,tr,mark):\n    payload=_obj(tr.get('payload')); pos=_obj(payload.get('position'))\n    ep=str(tr['episode_id']); account=str(tr['account_id']); asset=str(tr['asset'])\n    payload['state_restore']={'restore_id':RESTORE_ID,'status':'RESTORED_ACTIVE_CONTINUATION',\n                              'source_snapshot_id':_load(TARGET).get('snapshot_id')}\n    payload_text=json.dumps(payload,ensure_ascii=False,separators=(',',':'))\n    existing=c.execute('SELECT status FROM v85_episodes WHERE episode_id=?',(ep,)).fetchone()\n    if existing and str(existing['status'])!='OPEN':\n        raise RuntimeError('RESTORE_TARGET_EPISODE_EXISTS_NONOPEN:'+ep)\n    if not existing:\n        c.execute(\"\"\"INSERT INTO v85_episodes\n          (episode_id,account_id,asset,idea_id,policy_version,opened_at,closed_at,opening_event,closing_event,status,payload,net_pnl)\n          VALUES(?,?,?,?,?,?,NULL,?,NULL,'OPEN',?,NULL)\"\"\",\n          (ep,account,asset,str(tr.get('idea_id')),str(tr.get('policy_version')),\n           str(tr.get('opened_at')),str(tr.get('opening_event')),payload_text))\n    else:\n        c.execute(\"UPDATE v85_episodes SET status='OPEN',payload=? WHERE episode_id=?\",(payload_text,ep))\n    fee=str(payload.get('entry_fee') or '0')\n    c.execute(\"\"\"INSERT INTO v85_positions(account_id,asset,episode_id,payload,entry_fee,last_price,last_fx)\n                 VALUES(?,?,?,?,?,?,?)\"\"\",\n              (account,asset,ep,json.dumps(pos,ensure_ascii=False,separators=(',',':')),fee,str(mark),'1'))\n    side='BUY' if str(pos.get('direction'))=='LONG' else 'SHORT'\n    c.execute(\"\"\"INSERT INTO v85_orders(intent_id,episode_id,account_id,event_id,side,quantity,price,fee,reason,at)\n                 VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(intent_id) DO NOTHING\"\"\",\n              (_intent(ep),ep,account,str(tr.get('opening_event')),side,str(pos.get('quantity')),\n               str(pos.get('entry_price')),fee,'STATE_SNAPSHOT_RESTORE_OBSERVED_OPEN',str(tr.get('opened_at'))))\n    c.execute(\"\"\"INSERT INTO v85_path(episode_id,event_id,at,price) VALUES(?,?,?,?)\n                 ON CONFLICT(episode_id,event_id) DO NOTHING\"\"\",\n              (ep,str(tr.get('opening_event')),str(tr.get('opened_at')),str(pos.get('entry_price'))))\n    c.execute(\"\"\"INSERT INTO v85_path(episode_id,event_id,at,price) VALUES(?,?,?,?)\n                 ON CONFLICT(episode_id,event_id) DO NOTHING\"\"\",\n              (ep,_mark_event(ep),str(_load(TARGET).get('captured_at')),str(mark)))\n\ndef restore_pre_durable_state(ledger, *, allow_non_postgres=False):\n    if getattr(ledger,'dialect',None)!='postgres' and not allow_non_postgres:\n        return {'status':'SKIPPED_NON_POSTGRES','restore_id':RESTORE_ID}\n    target=_load(TARGET); archive=_load(ARCHIVE); accounts=_target_accounts(target)\n    marks={}\n    for pf in target.get('portfolios') or []:\n        for p in pf.get('positions') or []:\n            marks[(str(pf.get('name')),str(p.get('asset')))]=p.get('mark') or p.get('last_price') or p.get('entry_price')\n    target_trades=[x for x in (target.get('trades') or []) if str(x.get('status') or '').upper()=='OPEN']\n    applied_at=datetime.now(timezone.utc).isoformat()\n    with ledger.transaction() as c:\n        c.execute(\"\"\"CREATE TABLE IF NOT EXISTS v86_state_restore_audit(\n          restore_id TEXT PRIMARY KEY, applied_at TEXT NOT NULL, source_snapshot_id TEXT NOT NULL, payload TEXT NOT NULL)\"\"\")\n        c.execute(\"\"\"CREATE TABLE IF NOT EXISTS v86_state_archive(\n          snapshot_id TEXT PRIMARY KEY, captured_at TEXT NOT NULL, reason TEXT NOT NULL, payload TEXT NOT NULL)\"\"\")\n        for snap,reason in ((archive,'INTERRUPTED_PREVIOUS_EPHEMERAL_STATE'),\n                            (target,'PRE_DURABLE_CONTINUITY_SNAPSHOT')):\n            c.execute(\"\"\"INSERT INTO v86_state_archive(snapshot_id,captured_at,reason,payload)\n                         VALUES(?,?,?,?) ON CONFLICT(snapshot_id) DO NOTHING\"\"\",\n                      (str(snap.get('snapshot_id')),str(snap.get('captured_at')),reason,\n                       json.dumps(snap,ensure_ascii=False,separators=(',',':'))))\n        old=c.execute('SELECT payload FROM v86_state_restore_audit WHERE restore_id=?',(RESTORE_ID,)).fetchone()\n        if old:\n            return {'status':'ALREADY_APPLIED','restore_id':RESTORE_ID,**_obj(old['payload'])}\n\n        existing_accounts=c.execute('SELECT account_id FROM v85_accounts ORDER BY account_id').fetchall()\n        cold_start=(len(existing_accounts)==0)\n        replaced=[]; skipped=[]; fee_adjustment={}\n\n        if cold_start:\n            for a in accounts.values():\n                c.execute('INSERT INTO v85_accounts VALUES(?,?,?,?)',\n                          (a['account_id'],a['initial_equity'],a['realized_equity'],a['policy_version']))\n                c.execute('INSERT INTO v85_risk VALUES(?,?,?,?)',\n                          (a['account_id'],a['initial_equity'],'RESTORED',str(target.get('captured_at'))))\n            for tr in target_trades:\n                key=(str(tr.get('account_id')),str(tr.get('asset')))\n                _insert_target_episode(c,tr,marks.get(key))\n                replaced.append({'account':key[0],'asset':key[1],\n                                 'restored_episode_id':str(tr.get('episode_id')),\n                                 'replaced_episode_id':None})\n            cut=_cutover_payload(target,accounts)\n            c.execute('INSERT INTO v85_cutovers VALUES(?,?,?) ON CONFLICT(cutover_id) DO NOTHING',\n                      (CUTOVER_ID,str(target.get('captured_at')),\n                       json.dumps(cut,ensure_ascii=False,separators=(',',':'))))\n        else:\n            current={}\n            rows=c.execute(\"\"\"SELECT p.account_id,p.asset,p.episode_id,p.payload AS position_payload,p.entry_fee,\n                                    p.last_price,p.last_fx,e.idea_id,e.opened_at,e.status,e.payload AS episode_payload\n                             FROM v85_positions p JOIN v85_episodes e ON e.episode_id=p.episode_id\"\"\").fetchall()\n            for row in rows:\n                d=_rowdict(row); current[(str(d['account_id']),str(d['asset']))]=d\n            for tr in target_trades:\n                account=str(tr.get('account_id')); asset=str(tr.get('asset')); key=(account,asset)\n                old_payload=_obj(tr.get('payload')); old_pos=_obj(old_payload.get('position'))\n                cur=current.get(key)\n                if cur is None:\n                    skipped.append({'account':account,'asset':asset,'reason':'NO_CURRENT_POSITION_TO_REPLACE'})\n                    continue\n                cur_payload=_obj(cur.get('episode_payload')); cur_pos=_obj(cur.get('position_payload'))\n                same_context=(str(cur.get('idea_id'))==str(tr.get('idea_id')) and\n                              str(cur_pos.get('direction'))==str(old_pos.get('direction')) and\n                              str(cur_pos.get('horizon'))==str(old_pos.get('horizon')))\n                if not same_context:\n                    skipped.append({'account':account,'asset':asset,'reason':'CURRENT_CONTEXT_CHANGED'})\n                    continue\n                if str(cur.get('episode_id'))==str(tr.get('episode_id')):\n                    skipped.append({'account':account,'asset':asset,'reason':'ALREADY_TARGET_EPISODE'})\n                    continue\n                old_ep=str(tr['episode_id']); cur_ep=str(cur['episode_id'])\n                existing=c.execute('SELECT status FROM v85_episodes WHERE episode_id=?',(old_ep,)).fetchone()\n                if existing and str(existing['status'])!='OPEN':\n                    skipped.append({'account':account,'asset':asset,'reason':'TARGET_EPISODE_EXISTS_NONOPEN'})\n                    continue\n                cur_payload['state_restore']={'restore_id':RESTORE_ID,'status':'REPLACED_BY_PRE_DURABLE_EPISODE',\n                                              'source_snapshot_id':target.get('snapshot_id'),\n                                              'replacement_episode_id':old_ep}\n                c.execute(\"UPDATE v85_episodes SET status='MIGRATION_DUPLICATE',payload=? WHERE episode_id=?\",\n                          (json.dumps(cur_payload,ensure_ascii=False,separators=(',',':')),cur_ep))\n                c.execute('DELETE FROM v85_pending_exits WHERE episode_id=?',(cur_ep,))\n                c.execute('DELETE FROM v85_positions WHERE account_id=? AND asset=?',(account,asset))\n                old_fee=Decimal(str(old_payload.get('entry_fee') or '0'))\n                cur_fee=Decimal(str(cur.get('entry_fee') or '0'))\n                _insert_target_episode(c,tr,marks.get(key))\n                acc=c.execute('SELECT realized_equity FROM v85_accounts WHERE account_id=?',(account,)).fetchone()\n                if acc:\n                    revised=Decimal(str(acc['realized_equity']))+cur_fee-old_fee\n                    c.execute('UPDATE v85_accounts SET realized_equity=? WHERE account_id=?',(str(revised),account))\n                    fee_adjustment[account]=str(Decimal(fee_adjustment.get(account,'0'))+cur_fee-old_fee)\n                replaced.append({'account':account,'asset':asset,'restored_episode_id':old_ep,\n                                 'replaced_episode_id':cur_ep})\n            cut=_cutover_payload(target,accounts)\n            c.execute('INSERT INTO v85_cutovers VALUES(?,?,?) ON CONFLICT(cutover_id) DO NOTHING',\n                      (CUTOVER_ID,str(target.get('captured_at')),\n                       json.dumps(cut,ensure_ascii=False,separators=(',',':'))))\n\n        result={'cold_start':cold_start,'replaced_count':len(replaced),'skipped_count':len(skipped),\n                'replaced':replaced,'skipped':skipped,'fee_adjustment':fee_adjustment,\n                'target_snapshot_id':target.get('snapshot_id'),\n                'archived_snapshot_id':archive.get('snapshot_id'),\n                'learning_created':False,'synthetic_close_created':False}\n        c.execute('INSERT INTO v86_state_restore_audit VALUES(?,?,?,?)',\n                  (RESTORE_ID,applied_at,str(target.get('snapshot_id')),\n                   json.dumps(result,ensure_ascii=False,separators=(',',':'))))\n    return {'status':'APPLIED','restore_id':RESTORE_ID,**result}\n",encoding='utf-8')
p=root/'veritas_v86/application.py'
_s=p.read_text(encoding='utf-8')
_old="""    from veritas_v85.postgres import PostgresLedger
    if settings.sqlite_path:
        from veritas_v85.storage import SQLiteLedger;ledger=SQLiteLedger(settings.sqlite_path)
    else:ledger=PostgresLedger(os.environ.get('DATABASE_URL',''))
"""
_new="""    from veritas_v85.postgres import PostgresLedger
    dsn=os.environ.get('DATABASE_URL','').strip()
    if settings.mode=='paper':
        if not dsn: raise RuntimeError('PERSISTENCE_REQUIRED:DATABASE_URL missing for paper mode')
        ledger=PostgresLedger(dsn)
    elif settings.sqlite_path:
        from veritas_v85.storage import SQLiteLedger;ledger=SQLiteLedger(settings.sqlite_path)
    else:
        ledger=PostgresLedger(dsn)
"""
if _old not in _s: raise SystemExit('V86_FORCE_POSTGRES_ANCHOR_NOT_FOUND')
_s=_s.replace(_old,_new,1)
_old2="""        model.pg_init()
        from .state_restore import restore_pre_durable_state
"""
_new2="""        model.pg_init()
        with ledger.raw_connection() as _c:
            _required=('ledger_events','system_settings','product_alerts')
            _missing=[_t for _t in _required if _c.execute("SELECT to_regclass(%s) AS r",('public.'+_t,)).fetchone()['r'] is None]
        if _missing: raise RuntimeError('RESEARCH_SCHEMA_INIT_FAILED:'+','.join(_missing))
        print(canonical_json({'event':'V86_RESEARCH_SCHEMA_READY','tables':list(_required)}),flush=True)
        from .state_restore import restore_pre_durable_state
"""
if _old2 not in _s: raise SystemExit('V86_RESEARCH_SCHEMA_GUARD_ANCHOR_NOT_FOUND')
p.write_text(_s.replace(_old2,_new2,1),encoding='utf-8')
print('V86_FORCE_POSTGRES_PATCH_OK')

# 14) Durable state is active: enable independently verified non-crypto paper execution sources.
import runpy as _runpy, sys as _sys
_noncrypto_patch=root.parent/'v86_staged_execution_sources_patch.py'
if not _noncrypto_patch.is_file(): raise SystemExit('V86_NONCRYPTO_PATCH_MISSING')
_saved_argv=list(_sys.argv)
try:
    _sys.argv=[str(_noncrypto_patch),str(root)]
    _runpy.run_path(str(_noncrypto_patch),run_name='__main__')
finally:
    _sys.argv=_saved_argv
print('V86_NONCRYPTO_RUNTIME_ACTIVE')


# 15) Immutable durable closed-trade ledger + one-time recovery of the verified pre-durable NQ close.
# The ledger is append-only at application level: close and ledger write share the same financial transaction.
p=root/'veritas_v85/book.py'
_s=p.read_text(encoding='utf-8')
if 'import json,hashlib' not in _s:
    if 'import json\n' not in _s: raise SystemExit('CLOSED_LEDGER_JSON_IMPORT_ANCHOR_NOT_FOUND')
    _s=_s.replace('import json\n','import json,hashlib\n',1)
_helper_anchor='from .funding import accrued\n'
_helper=r'''
CLOSED_TRADE_LEDGER_CONTRACT="V86_CLOSED_TRADE_LEDGER_V1"
_CLOSED_TRADE_LEDGER_SCHEMA="""CREATE TABLE IF NOT EXISTS v86_closed_trade_ledger(
 ledger_id TEXT PRIMARY KEY,
 episode_id TEXT NOT NULL UNIQUE,
 account_id TEXT NOT NULL,
 asset TEXT NOT NULL,
 idea_id TEXT,
 policy_version TEXT,
 opened_at TEXT,
 closed_at TEXT NOT NULL,
 direction TEXT,
 horizon TEXT,
 setup_family TEXT,
 entry_nav TEXT,
 entry_price TEXT,
 exit_price TEXT,
 quantity TEXT,
 entry_fee TEXT,
 exit_fee TEXT,
 funding TEXT,
 gross_pnl TEXT,
 net_pnl TEXT NOT NULL,
 mfe_fraction TEXT,
 mae_fraction TEXT,
 giveback_fraction TEXT,
 held_seconds TEXT,
 exit_reason TEXT,
 path_points INTEGER,
 finalization_contract TEXT,
 learning_eligible INTEGER NOT NULL,
 record_kind TEXT NOT NULL,
 source_evidence TEXT,
 record_hash TEXT NOT NULL,
 payload TEXT NOT NULL,
 created_at TEXT NOT NULL
)"""

def ensure_closed_trade_ledger(c):
    c.execute(_CLOSED_TRADE_LEDGER_SCHEMA)

def _closed_trade_record(*,episode_id,account_id,asset,idea_id,policy_version,opened_at,closed_at,payload,net_pnl,
                         record_kind='CLOSED_FINAL',source_evidence=None):
    pay=payload if isinstance(payload,dict) else json.loads(payload or '{}')
    pos=pay.get('position') if isinstance(pay.get('position'),dict) else {}
    sig=pay.get('signal') if isinstance(pay.get('signal'),dict) else {}
    out=pay.get('outcome') if isinstance(pay.get('outcome'),dict) else {}
    return {
      'episode_id':str(episode_id),'account_id':str(account_id),'asset':str(asset),
      'idea_id':idea_id,'policy_version':policy_version,'opened_at':opened_at,'closed_at':str(closed_at),
      'direction':pos.get('direction') or sig.get('direction'),'horizon':pos.get('horizon') or sig.get('horizon'),
      'setup_family':sig.get('setup_family') or sig.get('setup'),
      'entry_nav':pay.get('entry_nav'),'entry_price':pos.get('entry_price'),'exit_price':out.get('exit_price'),
      'quantity':pos.get('quantity'),'entry_fee':out.get('entry_fee',pay.get('entry_fee')),
      'exit_fee':out.get('exit_fee'),'funding':out.get('funding_close_leg'),
      'gross_pnl':out.get('gross_close_leg'),'net_pnl':str(net_pnl),
      'mfe_fraction':out.get('observed_mfe_fraction'),'mae_fraction':out.get('observed_mae_fraction'),
      'giveback_fraction':out.get('giveback_from_observed_peak'),'held_seconds':out.get('held_seconds'),
      'exit_reason':out.get('exit_reason'),'path_points':out.get('path_points'),
      'finalization_contract':out.get('finalization_contract'),
      'learning_eligible':bool(out.get('learning_eligible')),
      'record_kind':record_kind,'source_evidence':source_evidence,
      'payload':pay,'created_at':str(closed_at)
    }

def append_closed_trade_record(c, record):
    ensure_closed_trade_ledger(c)
    rec=dict(record)
    episode_id=str(rec.get('episode_id') or '')
    if not episode_id: raise ValueError('CLOSED_LEDGER_EPISODE_REQUIRED')
    if rec.get('net_pnl') is None: raise ValueError('CLOSED_LEDGER_NET_REQUIRED')
    if not rec.get('closed_at'): raise ValueError('CLOSED_LEDGER_CLOSED_AT_REQUIRED')
    rec['ledger_id']=stable_id('CL86_',episode_id)
    rec['created_at']=str(rec.get('created_at') or rec['closed_at'])
    core={k:rec.get(k) for k in (
      'ledger_id','episode_id','account_id','asset','idea_id','policy_version','opened_at','closed_at',
      'direction','horizon','setup_family','entry_nav','entry_price','exit_price','quantity','entry_fee',
      'exit_fee','funding','gross_pnl','net_pnl','mfe_fraction','mae_fraction','giveback_fraction',
      'held_seconds','exit_reason','path_points','finalization_contract','learning_eligible','record_kind',
      'source_evidence','payload','created_at')}
    encoded=canonical_json(core)
    record_hash=hashlib.sha256(encoded.encode('utf-8')).hexdigest()
    prior=c.execute('SELECT record_hash FROM v86_closed_trade_ledger WHERE episode_id=?',(episode_id,)).fetchone()
    if prior:
        if str(prior['record_hash'])!=record_hash:
            raise RuntimeError('CLOSED_LEDGER_IMMUTABILITY_CONFLICT:'+episode_id)
        return False
    c.execute("""INSERT INTO v86_closed_trade_ledger(
      ledger_id,episode_id,account_id,asset,idea_id,policy_version,opened_at,closed_at,direction,horizon,setup_family,
      entry_nav,entry_price,exit_price,quantity,entry_fee,exit_fee,funding,gross_pnl,net_pnl,mfe_fraction,mae_fraction,
      giveback_fraction,held_seconds,exit_reason,path_points,finalization_contract,learning_eligible,record_kind,
      source_evidence,record_hash,payload,created_at)
      VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
      (core['ledger_id'],core['episode_id'],core['account_id'],core['asset'],core['idea_id'],core['policy_version'],
       core['opened_at'],core['closed_at'],core['direction'],core['horizon'],core['setup_family'],core['entry_nav'],
       core['entry_price'],core['exit_price'],core['quantity'],core['entry_fee'],core['exit_fee'],core['funding'],
       core['gross_pnl'],core['net_pnl'],core['mfe_fraction'],core['mae_fraction'],core['giveback_fraction'],
       core['held_seconds'],core['exit_reason'],core['path_points'],core['finalization_contract'],
       1 if core['learning_eligible'] else 0,core['record_kind'],
       canonical_json(core['source_evidence']) if core['source_evidence'] is not None else None,
       record_hash,canonical_json(core['payload']),core['created_at']))
    return True

def append_closed_trade_from_live(c,p,original,outcome,at,episode_net):
    pay=dict(original); pay['outcome']=dict(outcome)
    rec=_closed_trade_record(
      episode_id=p.episode_id,account_id=p.account_id,asset=p.asset,idea_id=p.idea_id,
      policy_version=p.policy_version,opened_at=p.opened_at.isoformat(),closed_at=at.isoformat(),
      payload=pay,net_pnl=episode_net,record_kind='CLOSED_FINAL',
      source_evidence={'source':'atomic_live_close','contract':CLOSED_TRADE_LEDGER_CONTRACT})
    return append_closed_trade_record(c,rec)

def backfill_closed_trade_ledger(ledger):
    inserted=0; existing=0
    with ledger.transaction() as c:
        ensure_closed_trade_ledger(c)
        rows=c.execute("SELECT * FROM v85_episodes WHERE status='CLOSED' ORDER BY closed_at,episode_id").fetchall()
        for row in rows:
            d=dict(row); pay=json.loads(d.get('payload') or '{}')
            rec=_closed_trade_record(
              episode_id=d['episode_id'],account_id=d['account_id'],asset=d['asset'],idea_id=d.get('idea_id'),
              policy_version=d.get('policy_version'),opened_at=d.get('opened_at'),closed_at=d.get('closed_at'),
              payload=pay,net_pnl=d.get('net_pnl'),record_kind='CLOSED_FINAL_BACKFILL',
              source_evidence={'source':'durable_v85_episodes_backfill','contract':CLOSED_TRADE_LEDGER_CONTRACT})
            if append_closed_trade_record(c,rec): inserted+=1
            else: existing+=1
    return {'status':'OK','inserted':inserted,'existing':existing,'contract':CLOSED_TRADE_LEDGER_CONTRACT}
'''
if 'CLOSED_TRADE_LEDGER_CONTRACT=' not in _s:
    if _helper_anchor not in _s: raise SystemExit('CLOSED_LEDGER_HELPER_ANCHOR_NOT_FOUND')
    _s=_s.replace(_helper_anchor,_helper_anchor+'\n'+_helper+'\n',1)
_close_anchor='        self.fault_hook("after_lesson_write")\n'
_close_inject="""        original['outcome']=outcome
        append_closed_trade_from_live(c,p,original,outcome,at,episode_net)
        self.fault_hook("after_lesson_write")
"""
if '        append_closed_trade_from_live(c,p,original,outcome,at,episode_net)\n' not in _s:
    if _close_anchor not in _s: raise SystemExit('CLOSED_LEDGER_CLOSE_ANCHOR_NOT_FOUND')
    _s=_s.replace(_close_anchor,_close_inject,1)
p.write_text(_s,encoding='utf-8')

# Read-only HTTP endpoint backed by the immutable ledger, independent of current positions/snapshots.
p=root/'veritas_v85/application.py'
_s=p.read_text(encoding='utf-8')
_route="""            if path=='/api/v1/portfolio-trades':
                try:return self.reply(app.trade_report(100),200)
                except Exception:return self.reply({'status':'UNAVAILABLE','error':'REPORT_READ_FAILED'},503)
"""
_route_new="""            if path=='/api/v1/closed-trade-ledger':
                try:
                    with app.ledger.read() as c:
                        rows=c.execute(\"\"\"SELECT ledger_id,episode_id,account_id,asset,idea_id,policy_version,opened_at,closed_at,
                          direction,horizon,setup_family,entry_nav,entry_price,exit_price,quantity,entry_fee,exit_fee,funding,
                          gross_pnl,net_pnl,mfe_fraction,mae_fraction,giveback_fraction,held_seconds,exit_reason,path_points,
                          finalization_contract,learning_eligible,record_kind,source_evidence,record_hash,payload,created_at
                          FROM v86_closed_trade_ledger ORDER BY closed_at DESC,created_at DESC,episode_id DESC LIMIT ?\"\"\",(1000,)).fetchall()
                    items=[]
                    for row in rows:
                        d=dict(row)
                        for key in ('payload','source_evidence'):
                            if isinstance(d.get(key),str):
                                try:d[key]=json.loads(d[key])
                                except Exception:pass
                        d['learning_eligible']=bool(d.get('learning_eligible'))
                        items.append(d)
                    return self.reply({'status':'OK','contract':'V86_CLOSED_TRADE_LEDGER_V1',
                                       'append_only':True,'count':len(items),'items':items},200)
                except Exception:return self.reply({'status':'UNAVAILABLE','error':'CLOSED_LEDGER_READ_FAILED'},503)
            if path=='/api/v1/portfolio-trades':
                try:return self.reply(app.trade_report(100),200)
                except Exception:return self.reply({'status':'UNAVAILABLE','error':'REPORT_READ_FAILED'},503)
"""
if "/api/v1/closed-trade-ledger" not in _s:
    if _route not in _s: raise SystemExit('CLOSED_LEDGER_ROUTE_ANCHOR_NOT_FOUND')
    _s=_s.replace(_route,_route_new,1)
    _s=_s.replace("'available':['/app','/readyz','/api/v85/snapshot','/api/v1/portfolio-trades']",
                  "'available':['/app','/readyz','/api/v85/snapshot','/api/v1/portfolio-trades','/api/v1/closed-trade-ledger']",1)
p.write_text(_s,encoding='utf-8')

# One-time, evidence-backed recovery. It affects accounting but is excluded from learning.
_recovery_src=root.parent/'recovery_evidence/VERITAS_v86_RECOVERED_CLOSE_EP_857bd4c759b192032369062acdfacf1a.json'
if not _recovery_src.is_file(): raise SystemExit('CLOSED_LEDGER_RECOVERY_EVIDENCE_MISSING')
(root/'veritas_v86/recovered_close_nq.json').write_text(_recovery_src.read_text(encoding='utf-8'),encoding='utf-8')
_recovery_module=r'''from __future__ import annotations
from pathlib import Path
from decimal import Decimal
from datetime import datetime,timezone
import json
from veritas_v85.book import append_closed_trade_record,ensure_closed_trade_ledger

HERE=Path(__file__).resolve().parent
EVIDENCE=HERE/'recovered_close_nq.json'

def recover_verified_historical_closes(ledger):
    e=json.loads(EVIDENCE.read_text(encoding='utf-8'))
    rid=str(e['recovery_id']); applied_at=datetime.now(timezone.utc).isoformat()
    with ledger.transaction() as c:
        ensure_closed_trade_ledger(c)
        c.execute("""CREATE TABLE IF NOT EXISTS v86_historical_recovery_audit(
          recovery_id TEXT PRIMARY KEY,applied_at TEXT NOT NULL,account_id TEXT NOT NULL,
          delta_realized_equity TEXT NOT NULL,payload TEXT NOT NULL)""")
        old=c.execute('SELECT payload FROM v86_historical_recovery_audit WHERE recovery_id=?',(rid,)).fetchone()
        if old:
            return {'status':'ALREADY_APPLIED','recovery_id':rid,**json.loads(old['payload'])}
        account=str(e['account_id'])
        acc=c.execute('SELECT realized_equity FROM v85_accounts WHERE account_id=?',(account,)).fetchone()
        if not acc: raise RuntimeError('RECOVERY_ACCOUNT_NOT_FOUND:'+account)
        delta=Decimal(str(e['account_delta_realized_equity']))
        record={
          'episode_id':e['episode_id'],'account_id':account,'asset':e['asset'],'idea_id':e.get('idea_id'),
          'policy_version':e.get('policy_version'),'opened_at':e.get('opened_at'),'closed_at':e['closed_at'],
          'direction':e.get('direction'),'horizon':e.get('horizon'),'setup_family':e.get('setup_family'),
          'entry_nav':e.get('entry_nav'),'entry_price':e.get('entry_price'),'exit_price':e.get('exit_price'),
          'quantity':e.get('quantity'),'entry_fee':e.get('entry_fee'),'exit_fee':e.get('exit_fee'),
          'funding':e.get('funding'),'gross_pnl':e.get('gross_pnl'),'net_pnl':e['net_pnl'],
          'mfe_fraction':None,'mae_fraction':None,'giveback_fraction':None,'held_seconds':None,
          'exit_reason':e.get('exit_reason'),'path_points':None,
          'finalization_contract':'RECOVERED_HISTORICAL_V1','learning_eligible':False,
          'record_kind':'RECOVERED_HISTORICAL_CLOSE','source_evidence':e,
          'payload':{'recovery':e,'learning_skip_reason':'PRE_DURABLE_PATH_NOT_RECOVERABLE'},
          'created_at':applied_at
        }
        inserted=append_closed_trade_record(c,record)
        if not inserted: raise RuntimeError('RECOVERY_LEDGER_ALREADY_EXISTS_WITHOUT_AUDIT:'+str(e['episode_id']))
        revised=Decimal(str(acc['realized_equity']))+delta
        c.execute('UPDATE v85_accounts SET realized_equity=? WHERE account_id=?',(str(revised),account))
        result={'episode_id':e['episode_id'],'account_id':account,'asset':e['asset'],
                'delta_realized_equity':str(delta),'realized_equity_after':str(revised),
                'learning_created':False,'record_kind':'RECOVERED_HISTORICAL_CLOSE'}
        c.execute('INSERT INTO v86_historical_recovery_audit VALUES(?,?,?,?,?)',
                  (rid,applied_at,account,str(delta),json.dumps(result,ensure_ascii=False,separators=(',',':'))))
    return {'status':'APPLIED','recovery_id':rid,**result}
'''
(root/'veritas_v86/closed_history_recovery.py').write_text(_recovery_module,encoding='utf-8')

# Run backfill/recovery before bootstrap. Both are idempotent and durable.
p=root/'veritas_v86/application.py'
_s=p.read_text(encoding='utf-8')
_boot_anchor="""        restore_result=restore_pre_durable_state(ledger)
        print(canonical_json({'event':'V86_STATE_RESTORE',**restore_result}),flush=True)
    app.bootstrap()
"""
_boot_new="""        restore_result=restore_pre_durable_state(ledger)
        print(canonical_json({'event':'V86_STATE_RESTORE',**restore_result}),flush=True)
        from veritas_v85.book import backfill_closed_trade_ledger
        closed_ledger_result=backfill_closed_trade_ledger(ledger)
        print(canonical_json({'event':'V86_CLOSED_LEDGER_BACKFILL',**closed_ledger_result}),flush=True)
        from .closed_history_recovery import recover_verified_historical_closes
        recovery_result=recover_verified_historical_closes(ledger)
        print(canonical_json({'event':'V86_HISTORICAL_CLOSE_RECOVERY',**recovery_result}),flush=True)
    app.bootstrap()
"""
if 'V86_HISTORICAL_CLOSE_RECOVERY' not in _s:
    if _boot_anchor not in _s: raise SystemExit('CLOSED_LEDGER_BOOT_ANCHOR_NOT_FOUND')
    _s=_s.replace(_boot_anchor,_boot_new,1)
p.write_text(_s,encoding='utf-8')
print('V86_CLOSED_TRADE_LEDGER_ACTIVE')
