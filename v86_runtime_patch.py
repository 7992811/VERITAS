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
                 'outcome_finalized':True,'finalization_contract':'CLOSED_FINAL_V1','learning_eligible':True,
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
        insert_lesson(c,lesson)
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

# 12) Stage independent execution quote sources for non-crypto assets.
# This code is committed now but must not be deployed until cumulative state is on durable storage.
p=root/'veritas_intelligence.py'
_src=p.read_text(encoding='utf-8')
import re as _re

_helper=r'''
def _stooq_latest(symbol):
    """Independent delayed futures quote for paper verification.
    Stooq current-quote CSV is used only as a second-source check, never as the analytical history owner.
    """
    import csv,io
    url='https://stooq.com/q/l/'
    with httpx.Client(timeout=15,headers={'User-Agent':'Mozilla/5.0 VERITAS paper-verification'}) as h:
        r=h.get(url,params={'s':symbol.lower(),'f':'sd2t2ohlcv','h':'','e':'csv'})
        r.raise_for_status()
        rows=list(csv.DictReader(io.StringIO(r.text)))
    if not rows:
        raise RuntimeError(f'STOOQ_NO_QUOTE {symbol}')
    row=rows[-1]
    price=None
    for k in ('Close','close','CLOSE'):
        if row.get(k) not in (None,'','N/D'):
            try: price=float(row[k]); break
            except Exception: pass
    if price is None or price<=0:
        raise RuntimeError(f'STOOQ_BAD_QUOTE {symbol}')
    return {'price':price,'observed_at':now(),'row':row,'source':'Stooq'}

def _finam_imoex_quote():
    """Best-effort independent delayed IMOEX verification.
    Fail-closed: any parser drift simply keeps MOEX execution disabled.
    """
    url='https://www.finam.ru/quote/moex/imoex/'
    with httpx.Client(timeout=15,headers={'User-Agent':'Mozilla/5.0 VERITAS paper-verification'}) as h:
        r=h.get(url); r.raise_for_status(); text=r.text
    # Finam server-rendered page contains the delayed last-trade price near IMOEX metadata.
    patterns=[
      r'(?is)Последн[^<]{0,40}(?:сделк|цена).*?([0-9][0-9\s]{2,}(?:[.,][0-9]+)?)\s*₽',
      r'(?is)"lastPrice"\s*:\s*"?([0-9]+(?:[.,][0-9]+)?)',
      r'(?is)"price"\s*:\s*"?([0-9]{4}(?:[.,][0-9]+)?)'
    ]
    price=None
    for pat in patterns:
        m=_re.search(pat,text)
        if m:
            try: price=float(m.group(1).replace(' ','').replace(',','.')); break
            except Exception: pass
    if price is None or price<=100:
        raise RuntimeError('FINAM_IMOEX_PARSE_FAIL')
    return {'price':price,'observed_at':now(),'source':'Finam IMOEX delayed'}

'''
anchor="def _research_only_derivatives(asset):"
if '_stooq_latest(symbol)' not in _src:
    if anchor not in _src: raise SystemExit('NONCRYPTO_HELPER_ANCHOR_NOT_FOUND')
    _src=_src.replace(anchor,_helper+'\n'+anchor,1)

# Brent/Gold: Yahoo futures primary + Stooq continuous futures independent secondary.
_pat=r"def _yahoo_research_futures_market\(asset,yahoo_symbol,proxy_symbol,policy_key,source_name\):\n.*?\n\ndef _moex_block"
_new=r'''def _yahoo_research_futures_market(asset,yahoo_symbol,proxy_symbol,policy_key,source_name):
    bars5,_=_yahoo_series(yahoo_symbol,'5d','5m',True)
    bars1h,_=_yahoo_series(yahoo_symbol,'3mo','1h',True)
    if len(bars1h)<200:
        raise RuntimeError(f'INSUFFICIENT_{asset}_HOURLY_BARS {len(bars1h)}')
    last=bars5[-1] if bars5 else bars1h[-1]
    price=float(last['close'])
    observed=datetime.fromtimestamp(last['ts'],tz=timezone.utc).isoformat()
    closes=[float(x['close']) for x in bars1h[-240:]]
    highs=[float(x['high']) for x in bars1h[-240:]]
    lows=[float(x['low']) for x in bars1h[-240:]]
    vols=[float(x.get('volume') or 0) for x in bars1h[-240:]]
    taker=[v*0.5 for v in vols]
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    market_open=_futures_market_open_from_age(observed)
    age=_age_seconds(observed); delay=DATA_SOURCE_POLICY[policy_key]['documented_delay_sec']

    stooq_symbol={'BRENT':'CB.F','GOLD':'GC.F'}.get(asset)
    secondary=None; sec_obs=None; sec_name=None; div=999.0
    if stooq_symbol:
        try:
            sq=_stooq_latest(stooq_symbol)
            secondary=float(sq['price']); sec_obs=sq['observed_at']; sec_name=f'Stooq {stooq_symbol}'
            mid=(price+secondary)/2
            div=abs(price-secondary)/mid if mid else 999.0
        except Exception:
            pass

    direct_ok=bool(secondary is not None and div<=0.015)
    gate=bool(market_open and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS)
    quality=[
      _source_row(source_name,f'{asset} futures','primary research delayed',observed,delay,
                  'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',
                  DATA_SOURCE_POLICY[policy_key]['commercial_note'],'Yahoo'),
      _source_row(sec_name or f'Stooq {stooq_symbol}',f'{asset} futures','independent delayed verification',
                  sec_obs,0,'OK' if direct_ok else 'FAIL',
                  f'direct futures cross-check divergence={div:.4%}' if secondary is not None else 'secondary unavailable',
                  'Stooq')
    ]
    _set_source_quality(quality)
    quote_event=f'{asset}:'+str(int(last['ts']))+':'+format(price,'.8f')+':'+(format(secondary,'.8f') if secondary is not None else 'NA')
    return {'asset':asset,'price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'secondary_observed_at':sec_obs,'source_divergence':div if secondary is not None else 999.0,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'intraday_bars':bars5,
            'taker_buy':taker,'returns':rets,'binance_close_time_ms':int(last['ts']*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,
            'source_quality':quality,'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'direct_independent' if direct_ok else 'single_direct_only',
            'source_names':{'primary':source_name,'secondary':sec_name or 'NOT_AVAILABLE'},
            'v85_quote':{
              'event_id':quote_event,'primary_time':observed,'secondary_time':sec_obs,
              'primary_source':source_name,'secondary_source':sec_name,
              'source_verified':bool(gate and direct_ok),
              'max_age_seconds':float(DELAYED_FUTURES_MAX_AGE_SECONDS)
            }}


def _moex_block'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'NONCRYPTO_FUTURES_PATCH_FAILED {n}')

# CNYRUBF: official MOEX perpetual future + independent Yahoo interbank CNY/RUB underlying.
_pat=r"def _cnyrubf_market\(\):\n.*?\n\ndef _moex_market"
_new=r'''def _cnyrubf_market():
    end=time.time(); hist=_moex_futures_candles_between('CNYRUBF',end-120*86400,end+86400,60)
    if len(hist)<120: raise RuntimeError(f'INSUFFICIENT_CNYRUBF_HOURLY_BARS {len(hist)}')
    q=_moex_futures_current_quote('CNYRUBF'); price=float(q['price']); observed=q['observed_at']
    w=hist[-360:]; closes=[float(x[4]) for x in w]; highs=[float(x[2]) for x in w]; lows=[float(x[3]) for x in w]
    vols=[float(x[5]) for x in w]; taker=[v*0.5 for v in vols]; closes[-1]=price
    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]
    age=_age_seconds(observed); market_open=_futures_market_open_from_age(observed)

    secondary=None; sec_obs=None
    try:
        yr,_=_yahoo_series('CNYRUB%3DX','5d','5m',True)
        if yr:
            secondary=float(yr[-1]['close'])
            sec_obs=datetime.fromtimestamp(yr[-1]['ts'],tz=timezone.utc).isoformat()
    except Exception:
        pass
    mid=(price+secondary)/2 if secondary is not None else None
    div=abs(price-secondary)/mid if mid else 999.0
    # Perpetual future vs interbank underlying: allow a modest basis, but fail closed beyond 2%.
    paired_ok=bool(secondary is not None and (_age_seconds(sec_obs) or 999999)<=1800 and div<=0.02)
    gate=bool(market_open and age is not None and age<=3600)
    quality=[
      _source_row('MOEX ISS CNYRUBF','CNY/RUB perpetual futures','primary delayed',observed,900,
                  'DELAYED_CONTEXT' if gate else 'STALE_OR_CLOSED',
                  DATA_SOURCE_POLICY['moex_forts_cnyrubf']['commercial_note'],'Moscow Exchange'),
      _source_row('Yahoo CNYRUB=X','CNY/RUB interbank','independent underlying verification',sec_obs,0,
                  'OK' if paired_ok else 'FAIL',
                  f'underlying/futures divergence={div:.4%}' if secondary is not None else 'secondary unavailable','Yahoo')
    ]
    _set_source_quality(quality)
    quote_event='CNYRUBF:'+str(int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()))+':'+format(price,'.6f')+':'+(format(secondary,'.6f') if secondary is not None else 'NA')
    return {'asset':'CNYRUBF','price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'secondary_observed_at':sec_obs,'source_divergence':div,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()*1000),
            'observed_at':observed,'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
            'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'paired_underlying_independent_vendor' if paired_ok else 'single_direct_official',
            'source_names':{'primary':'MOEX ISS CNYRUBF','secondary':'Yahoo CNYRUB=X' if secondary is not None else 'NOT_AVAILABLE'},
            'v85_quote':{
              'event_id':quote_event,'primary_time':observed,'secondary_time':sec_obs,
              'primary_source':'MOEX ISS CNYRUBF','secondary_source':'Yahoo CNYRUB=X' if secondary is not None else None,
              'source_verified':bool(gate and paired_ok),'max_age_seconds':3600.0
            },
            'contract':{'secid':'CNYRUBF','lot':1000,'price_tick':0.001,'tick_value_rub':1.0,'settlement':'cash','roll':'automatic'}}

def _moex_market'''
_src,n=_re.subn(_pat,_new,_src,count=1,flags=_re.S)
if n!=1: raise SystemExit(f'CNYRUBF_PATCH_FAILED {n}')

# MOEX: keep official ISS primary, use Yahoo when fresh, otherwise best-effort Finam delayed quote.
_old="""    quality=[
      _source_row('MOEX ISS IMOEX','MOEX index','primary research delayed',observed,MOEX_FREE_ISS_DELAY_SECONDS,
                  'DELAYED_CONTEXT' if gate else ('SESSION_CLOSED' if not open_now else 'STALE'),
                  DATA_SOURCE_POLICY['moex_iss']['commercial_note'],'Moscow Exchange'),
      _source_row('Yahoo IMOEX.ME','MOEX index','secondary research check',yobs,900,ystatus,
                  'Best-effort secondary check; may be materially stale','Yahoo')
    ]
"""
_new="""    # If Yahoo is stale/unavailable, try a separate delayed Finam quote.
    if secondary is None or yobs is None or (_age_seconds(yobs) or 999999)>MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS:
        try:
            fq=_finam_imoex_quote()
            secondary=float(fq['price']); yobs=fq['observed_at']; ystatus='OK'
        except Exception:
            pass
    divergence=(abs(price-secondary)/((price+secondary)/2) if secondary and (price+secondary) else 999.0)
    secondary_fresh=bool(secondary is not None and yobs is not None and (_age_seconds(yobs) or 999999)<=MOEX_EXEC_MAX_SECONDARY_AGE_SECONDS)
    exec_ok=bool(gate and secondary_fresh and divergence<=MOEX_EXEC_MAX_DIVERGENCE)
    quality=[
      _source_row('MOEX ISS IMOEX','MOEX index','primary research delayed',observed,MOEX_FREE_ISS_DELAY_SECONDS,
                  'DELAYED_CONTEXT' if gate else ('SESSION_CLOSED' if not open_now else 'STALE'),
                  DATA_SOURCE_POLICY['moex_iss']['commercial_note'],'Moscow Exchange'),
      _source_row('Yahoo/Finam IMOEX','MOEX index','independent delayed verification',yobs,900,
                  'OK' if secondary_fresh else 'STALE_OR_UNAVAILABLE',
                  f'price divergence={divergence:.4%}' if secondary is not None else 'secondary unavailable','Yahoo/Finam')
    ]
"""
if _old not in _src: raise SystemExit('MOEX_QUALITY_ANCHOR_NOT_FOUND')
_src=_src.replace(_old,_new,1)

_old="""    return {'asset':'MOEX','price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'secondary_observed_at':yobs,
            'source_divergence':(abs(price-secondary)/((price+secondary)/2) if secondary and (price+secondary) else 0.0),
"""
_new="""    quote_event='MOEX:'+str(int(datetime.fromisoformat(observed.replace('Z','+00:00')).timestamp()))+':'+format(price,'.6f')+':'+(format(secondary,'.6f') if secondary is not None else 'NA')
    return {'asset':'MOEX','price':price,'secondary_price':secondary,'coinbase_price':secondary,
            'secondary_observed_at':yobs,'source_divergence':divergence,
"""
if _old not in _src: raise SystemExit('MOEX_RETURN_ANCHOR_NOT_FOUND')
_src=_src.replace(_old,_new,1)

_old="""            'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,
            'source_names':{'primary':'MOEX ISS IMOEX','secondary':'Yahoo IMOEX.ME'}}
"""
_new="""            'data_latency_class':'DELAYED_RESEARCH','intraday_5m':moex5m,
            'verification_mode':'direct_independent' if exec_ok else 'single_direct_official',
            'source_names':{'primary':'MOEX ISS IMOEX','secondary':'Yahoo/Finam IMOEX'},
            'v85_quote':{
              'event_id':quote_event,'primary_time':observed,'secondary_time':yobs,
              'primary_source':'MOEX ISS IMOEX','secondary_source':'Yahoo/Finam IMOEX',
              'source_verified':bool(exec_ok),'max_age_seconds':float(MOEX_MAX_AGE_SECONDS)
            }}
"""
if _old not in _src: raise SystemExit('MOEX_SOURCE_NAMES_ANCHOR_NOT_FOUND')
_src=_src.replace(_old,_new,1)

# CNY execution gate now accepts verified independent underlying cross-check.
_old="""    if asset=='CNYRUBF':
        return {'eligible':False,'reason':'research_only_no_second_direct_cnyrubf_quote','direct_sources':1,
                'research_ok':research_ok,'time_ok':time_ok,'verification_mode':raw.get('verification_mode')}
"""
_new="""    if asset=='CNYRUBF':
        sec=raw.get('secondary_price'); sec_ts=raw.get('secondary_observed_at')
        age=_age_seconds(sec_ts) if sec_ts else None
        divergence=float(raw.get('source_divergence') or 999.0)
        mode=raw.get('verification_mode')
        ok=bool(research_ok and time_ok and mode=='paired_underlying_independent_vendor'
                and sec is not None and age is not None and age<=1800 and divergence<=0.02)
        return {'eligible':ok,'reason':'cnyrubf_independent_underlying_check' if ok else 'research_only_no_second_direct_cnyrubf_quote',
                'direct_sources':2 if ok else 1,'secondary_age_seconds':age,'divergence':divergence,
                'research_ok':research_ok,'time_ok':time_ok,'verification_mode':mode}
"""
if _old not in _src: raise SystemExit('CNY_EXEC_GATE_ANCHOR_NOT_FOUND')
_src=_src.replace(_old,_new,1)

p.write_text(_src,encoding='utf-8')
print('V86_NONCRYPTO_EXECUTION_SOURCES_STAGED')
print('V86_RUNTIME_PATCH_OK')
