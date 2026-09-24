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

print('V86_RUNTIME_PATCH_OK')
