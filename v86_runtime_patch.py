from pathlib import Path
import sys

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
                 'observed_mfe_fraction':str(mfe),'observed_mae_fraction':str(mae),
"""
if old not in s: raise SystemExit('BOOK_OUTCOME_ANCHOR_NOT_FOUND')
p.write_text(s.replace(old,new),encoding='utf-8')

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
    return {'asset':'NQ','price':price,'secondary_price':sec,'coinbase_price':sec,
            'secondary_observed_at':sec_ts,'source_divergence':div,
            'closes':closes,'highs':highs,'lows':lows,'vols':vols,'taker_buy':taker,'returns':rets,
            'binance_close_time_ms':int(pbar['ts']*1000),'observed_at':pts,
            'source_gate_pass':gate,'market_open':market_open,'source_quality':quality,
            'intraday_bars':nq5,'daily_bars':nqdaily,'volume_intraday_bars':nq5,
            'data_latency_class':'DELAYED_RESEARCH',
            'verification_mode':'paired_contract_same_underlying',
            'source_names':{'primary':'Yahoo CME NQ=F','secondary':'Yahoo CME MNQ=F'},
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
print('V86_RUNTIME_PATCH_OK')
