"""Incremental closed-paper-trade learning, isolated from trading mutations.

Each process call runs ONE bounded phase. Durable job progress advances only
when the work succeeds; AUTO owns atomic observation deduplication. Legacy
trades may repair R29 diagnostics, but missing prospective stamps are never
reconstructed from the current policy or model.
"""
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import math
import time
import veritas_learning_exports as EXPORTS

import veritas_learning_integrity as LI
import veritas_learning_state as STORE
import veritas_trade_audit as AUDIT
import veritas_autonomous_learning as AUTO
import veritas_price_source as SOURCE

VERSION = "CLOSED_TRADE_MICROBATCH_V1"
JOB_NAME = "closed_trade_learning"
BATCH_SIZE = 4
MAX_BATCH_SIZE = 8
EPOCH = "2026-09-26T07:13:08+00:00"


def _number(value):
    if value is None or isinstance(value, bool):
        return None
    try:
        value=float(value)
        return value if math.isfinite(value) else None
    except (ValueError,TypeError,OverflowError):
        return None


def _time(value):
    stamp=AUDIT._timestamp(value)
    return datetime.fromtimestamp(stamp,timezone.utc) if stamp is not None else None


def _hash(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),default=str,allow_nan=False).encode()).hexdigest()


def observation(trade, *, now=None):
    """Freeze one size experiment in common baseline net-stop-risk units.

    A normalized paper quantity is not a broker contract quantity. The risk
    denominator is its ORIGINAL persisted cost-inclusive stop budget, not a
    price-only return and not each alternative's separately normalized risk.
    """
    t=dict(trade or {}); p=AUDIT.payload(t.get('payload'))
    admission=p.get('entry_canonical_admission') or {}
    admission=admission if isinstance(admission,dict) else {}
    stamp=admission.get('autonomous_learning') or {}
    if not isinstance(stamp,dict) or not stamp.get('policy_hash'):
        return None,'MISSING_PROSPECTIVE_POLICY_STAMP'
    integrity=t.get('learning_integrity') or {}
    if not isinstance(integrity,dict): integrity={}
    exclusion=integrity.get('exclusion_reason') or t.get('learning_exclusion_reason')
    if exclusion in ('DUPLICATE_OBSERVED_EVENT','DUPLICATE_MARKET_EPISODE'):
        return None,'DUPLICATE_MARKET_IDEA'
    event=p.get('entry_event_snapshot') or {}
    if not isinstance(event,dict) or not event.get('event_id'):
        return None,'MISSING_ORIGINAL_EVENT'
    source=p.get('price_source_lock') or p.get('entry_execution_source_identity')
    frozen_source=stamp.get('source_identity')
    source_ok=bool(isinstance(source,dict) and isinstance(frozen_source,dict)
                   and SOURCE.same(source,frozen_source) and SOURCE.same(frozen_source,source))
    opened,closed=_time(t.get('opened_at')),_time(t.get('closed_at'))
    decision=_time(stamp.get('decision_at')); known=_time(event.get('signal_at'))
    clock=_time(now) if now is not None else datetime.now(timezone.utc)
    # Include the entire prospective stamp and original risk inputs so a later
    # correction also revokes an already consumed autonomous observation.
    evidence_hash=_hash([t.get('learning_evidence_hash'),stamp,
        {k:p.get(k) for k in ('normalized_units','entry_nav_rub','entry_stop_risk_budget',
                              'initial_stop_price','entry_execution_model','last_add_event_id')}])
    common=dict(idea_id=event['event_id'],event_id=event['event_id'],asset=t.get('asset'),
        horizon=t.get('horizon'),direction=t.get('direction'),regime=stamp.get('regime') or 'UNKNOWN',
        policy_hash=stamp['policy_hash'],source_identity=frozen_source,
        source_verified=source_ok,independence_verified=True,
        evidence_hash=evidence_hash,evidence_version=LI.VERSION,
        proof_kind='SIMULATED_SIZE_ON_OBSERVED_PATH',
        known_at=known.isoformat() if known else None,decision_at=decision.isoformat() if decision else None,
        outcome_at=closed.isoformat() if closed else None,observed_at=clock.isoformat() if clock else None,
        predicted_probability=_number(stamp.get('base_probability')))
    problem=LI.trade_exclusion(t)
    verified=bool(t.get('episode_eligible') is True and integrity.get('version')==LI.VERSION
                  and integrity.get('status')=='VERIFIED'
                  and integrity.get('evidence_hash')==t.get('learning_evidence_hash')
                  and integrity.get('event_id')==event['event_id'])
    if problem or not verified or not source_ok:
        # This is an explicit revocation record, not positive training evidence.
        return dict(common,evidence_valid=False),problem or 'UNVERIFIED_CURRENT_EPISODE'
    if not all((opened,closed,decision,known,clock)) or not known <= decision <= opened < closed <= clock:
        return None,'INVALID_FROZEN_TIMES'
    if p.get('last_add_event_id') or p.get('last_add_stop_risk_budget') or p.get('last_add_canonical_admission'):
        return None,'MULTIFILL_SIZE_REPLAY_REQUIRED'
    if (p.get('normalized_paper_notional') is not True
            or p.get('quantity_semantics')!='NORMALIZED_PAPER_RETURN_UNITS'):
        return None,'NORMALIZED_PAPER_QUANTITY_REQUIRED'
    units,nav=_number(p.get('normalized_units')),_number(p.get('entry_nav_rub'))
    fill=_number((p.get('entry_execution_model') or {}).get('fill_price'))
    stop=_number(p.get('initial_stop_price'))
    budget=p.get('entry_stop_risk_budget') or {}
    if not isinstance(budget,dict): return None,'ORIGINAL_NET_STOP_BUDGET_REQUIRED'
    marginal,cap=_number(budget.get('marginal_net_risk_pct')),_number(budget.get('risk_cap_nav'))
    before,after=_number(stamp.get('fraction_before')),_number(stamp.get('fraction_after'))
    if (any(x is None or x<=0 for x in (units,nav,fill,stop,marginal,cap,before,after))
            or after>before+1e-12 or budget.get('version')!='NET_STOP_RISK_BUDGET_V1'
            or budget.get('eligible') is not True or budget.get('current_fraction')!=0):
        return None,'ORIGINAL_NET_STOP_BUDGET_REQUIRED'
    actual_fraction=units*fill/nav
    actual_risk=units*fill*marginal
    signed_distance=(fill-stop)*(1 if t['direction']=='LONG' else -1)
    execution_audit=stamp.get('execution_audit')
    if (not math.isclose(actual_fraction,after,rel_tol=1e-6,abs_tol=1e-8)
            or (isinstance(execution_audit,dict) and (execution_audit.get('actual_effect_verified') is False or execution_audit.get('executed_as_proposed') is False))):
        return None,'POST_ADMISSION_ALLOCATION_CHANGED_REPLAY_REQUIRED'
    if (isinstance(execution_audit,dict) and _number(execution_audit.get('actual_fraction')) is not None
            and not math.isclose(actual_fraction,float(execution_audit['actual_fraction']),rel_tol=1e-6,abs_tol=1e-8)):
        return None,'ACCOUNTED_SIZE_AUDIT_MISMATCH'
    if signed_distance<=0 or marginal+1e-12<signed_distance/fill or actual_risk>cap*nav+1e-8:
        return None,'ORIGINAL_SIZE_OR_STOP_RISK_MISMATCH'
    gross,fees,funding,net=(_number(t.get(k)) for k in ('gross_pnl_rub','fees_rub','funding_rub','net_pnl_rub'))
    if (any(x is None for x in (gross,fees,funding,net)) or fees<0 or funding<0
            or not math.isclose(gross-fees-funding,net,rel_tol=1e-8,abs_tol=1e-6)):
        return None,'CASHFLOW_RECONCILIATION_REQUIRED'
    # Recover the baseline exposure only from its immutable before/after stamp.
    actual_multiplier=after/before
    baseline_risk_rub=actual_risk/actual_multiplier
    baseline_net_r=(net/actual_multiplier)/baseline_risk_rub
    risk_cap_r=cap*nav/baseline_risk_rub
    if risk_cap_r<1.-1e-8:
        return None,'BASELINE_EXCEEDS_FROZEN_RISK_CAP'
    probability=common['predicted_probability']
    if probability is None or not 0<=probability<=1:
        return None,'MISSING_FROZEN_PROBABILITY'
    candidates=stamp.get('prospective_candidates') or []
    if not isinstance(candidates,list) or len(candidates)>8:
        return None,'INVALID_PROSPECTIVE_CANDIDATES'
    candidates=[x for x in candidates if isinstance(x,dict) and x.get('kind')=='SIZE_DOWN_WEAK_SIGNAL']
    if len(candidates)>1:
        return None,'AMBIGUOUS_PROSPECTIVE_SIZE_TRIAL'
    candidate=candidates[0] if candidates else {}
    execution=candidate.get('size_execution')
    factor=1.
    if candidate:
        created,stamped=_time(candidate.get('created_at')),_time(candidate.get('candidate_decision_at'))
        if (not created or not stamped or not created<stamped<=decision
                or candidate.get('scope')!=AUTO.scope_for(common)
                or candidate.get('base_probability')!=probability):
            return None,'PROSPECTIVE_CANDIDATE_SCOPE_MISMATCH'
        expected=AUTO.size_execution(probability,fraction=before,step=stamp.get('position_step'),
                                     cap=execution.get('max_fraction') if isinstance(execution,dict) else None)
        if expected is None or execution!=expected:
            return None,'INVALID_FROZEN_SIZE_EXECUTION'
        factor=execution['effective_multiplier']
    result=dict(common,evidence_valid=True,costs_verified=True,risk_verified=True,
        baseline_net_r=baseline_net_r,candidate_net_r=baseline_net_r*factor,
        baseline_risk_r=1.,candidate_risk_r=factor,risk_cap_r=risk_cap_r,
        net_r=baseline_net_r,candidate_id=candidate.get('candidate_id'),
        candidate_decision_at=candidate.get('candidate_decision_at'))
    if execution is not None: result['size_execution']=execution
    return result,None


def _bounded_object(expression, maximum=32768):
    return "CASE WHEN jsonb_typeof("+expression+")='object' AND octet_length(("+expression+")::text)<="+str(maximum)+" THEN "+expression+" ELSE NULL END"


def payload_projection(alias='t'):
    # Keep original evidence intact through the shared LI projection. Only a
    # small explicit set of scalar economics and prospective stamps is added.
    LI._alias(alias)
    root=alias+'.payload'
    scalars=('expected_move_pct','expected_to_stop_ratio','opening_fraction','entry_nav_rub',
        'entry_probability','pwin','setup_family','regime','entry_regime','entry_signal_tier',
        'setup_grade','canonical_setup_id','setup_id','normalized_units','normalized_paper_notional',
        'quantity_semantics','last_add_event_id')
    extra=["'"+k+"',"+LI._proof_scalar(root+"->'"+k+"'") for k in scalars]
    for key in ('entry_canonical_admission','entry_stop_risk_budget','last_add_stop_risk_budget','last_add_canonical_admission'):
        extra.append("'"+key+"',"+_bounded_object(root+"->'"+key+"'"))
    return LI.payload_sql(alias)+'||jsonb_build_object('+','.join(extra)+')'


class TradeLearning:
    def __init__(self, namespace):
        self.ns=namespace
        self._verified_epoch=None
    def _check(self,context):
        if context is not None:context.check()
    @contextmanager
    def _transaction(self,context, *, revalidate=False):
        self._check(context)
        with self.ns['pg_connect']() as c:
            boundary=LI.revalidation_transaction(c) if revalidate else c.transaction()
            with boundary:
                timeout=max(1,min(2000,int(getattr(context,'sql_timeout_ms',2000))))
                c.execute("SET LOCAL statement_timeout = '"+str(timeout)+"ms'")
                c.execute("SET LOCAL lock_timeout = '250ms'")
                yield c
                self._check(context)
    def _projection(self):
        fields=','.join('t.'+key for key in LI.TRADE_FIELDS)
        return fields+',t.portfolio_name,t.setup,t.max_fraction,t.avg_entry_price,t.avg_exit_price,'+payload_projection()+' AS payload'
    def ensure_schema(self,context=None):
        with self._transaction(context) as c:
            getattr(self.ns['VP'],'_v90r29_ensure')(c)
            c.execute('''CREATE TABLE IF NOT EXISTS learning_trade_receipts (
                trade_id text PRIMARY KEY,asset text NOT NULL,event_id text NOT NULL,
                evidence_hash text NOT NULL,observation jsonb NOT NULL,valid boolean NOT NULL DEFAULT TRUE,
                updated_at timestamptz NOT NULL DEFAULT now(),UNIQUE(asset,event_id),
                CHECK(octet_length(observation::text)<=32768))''')
        return {'status':'OK'}
    def process(self,context=None):
        pg=self.ns['pg_connect'];self._check(context)
        lease=STORE.claim_job(pg,JOB_NAME,VERSION,lease_seconds=60)
        if not lease:return {'status':'DEFERRED_BUSY','job':JOB_NAME}
        cursor=dict(lease.get('cursor') or {});phase=cursor.get('phase','materialize')
        result={'status':'OK','phase':phase,'batch_limit':BATCH_SIZE}
        published_snapshot=None
        try:
            if phase=='materialize':
                with self._transaction(context) as c:
                    rows=c.execute('SELECT '+self._projection()+''' FROM paper_trades t
                        LEFT JOIN v90_learning_episodes e ON e.trade_id=t.trade_id
                        WHERE e.trade_id IS NULL AND t.closed_at IS NOT NULL
                          AND t.status IN ('CLOSED','CLOSE','EXITED') AND t.opened_at>=%s::timestamptz
                        ORDER BY t.closed_at,t.trade_id LIMIT %s FOR UPDATE OF t SKIP LOCKED''',
                        (EPOCH,BATCH_SIZE)).fetchall()
                    upsert=getattr(self.ns['VP'],'_v90r29_upsert_episode')
                    for row in rows:
                        self._check(context)
                        if not upsert(c,dict(row)):
                            raise RuntimeError('closed trade episode materialization rejected')
                    if rows:LI.invalidate('closed_trade_materialized',new_evidence_only=True)
                result['materialized']=len(rows);cursor['phase']='revalidate'
            elif phase=='revalidate':
                with self._transaction(context,revalidate=True) as c:
                    result['validation']=LI.revalidate_eligible(c,batch_size=BATCH_SIZE)
                cursor['phase']='export'
            elif phase=='export':
                after=cursor.get('after') or [EPOCH,'']
                with self._transaction(context) as c:
                    rows=c.execute('SELECT '+self._projection()+''',e.learning_eligible AS episode_eligible,
                        e.payload->'learning_integrity' AS learning_integrity,
                        e.payload->>'learning_exclusion_reason' AS learning_exclusion_reason,'''+
                        LI.evidence_hash_sql('t')+''' AS learning_evidence_hash
                        FROM paper_trades t LEFT JOIN v90_learning_episodes e ON e.trade_id=t.trade_id
                        WHERE t.closed_at IS NOT NULL AND t.status IN ('CLOSED','CLOSE','EXITED')
                          AND t.opened_at>=%s::timestamptz
                          AND (t.closed_at,t.trade_id)>(%s::timestamptz,%s)
                        ORDER BY t.closed_at,t.trade_id LIMIT %s''',(EPOCH,*after,BATCH_SIZE)).fetchall()
                observations=[];exclusions=Counter()
                clock=datetime.now(timezone.utc)
                for raw in rows:
                    self._check(context)
                    item,reason=observation(dict(raw),now=clock)
                    if item is not None:
                        with self._transaction(context) as c:
                            receipt=c.execute('SELECT trade_id FROM learning_trade_receipts WHERE asset=%s AND event_id=%s',
                                              (item['asset'],item['event_id'])).fetchone()
                            if receipt and receipt['trade_id']!=raw['trade_id']:
                                item=None;reason='DUPLICATE_PORTFOLIO_REPRESENTATION'
                            elif not receipt and item.get('evidence_valid') is True:
                                c.execute('''INSERT INTO learning_trade_receipts
                                    (trade_id,asset,event_id,evidence_hash,observation) VALUES(%s,%s,%s,%s,%s::jsonb)
                                    ON CONFLICT DO NOTHING''',(raw['trade_id'],item['asset'],item['event_id'],
                                                               item['evidence_hash'],STORE._json(item,16384)))
                            elif not receipt:
                                item=None  # Unused rejected evidence cannot revoke another portfolio's idea.
                        if item is not None:observations.append(item)
                    if reason:exclusions[reason]+=1
                if observations:
                    published_snapshot=AUTO.run_batch(pg,observations,now=clock,context=context)
                    result['autonomous']=published_snapshot
                    # Only bounded progress belongs in the job row; the full
                    # candidate snapshot already has its own durable slot.
                    result['autonomous']={k:result['autonomous'].get(k) for k in ('status','counts','candidate_counts')}
                result.update(scanned=len(rows),submitted=len(observations),exclusions=dict(exclusions))
                cursor['after']=[rows[-1]['closed_at'].isoformat(),rows[-1]['trade_id']] if rows else [EPOCH,'']
                cursor['phase']='recheck'
            elif phase=='recheck':
                state=LI.memory_state();epoch=[state['process_epoch'],state['revocation_generation']]
                if not state['ready']:
                    cursor['phase']='revalidate'
                    result['reason']='AWAIT_COMMITTED_VALIDATION'
                else:
                    if cursor.get('recheck_epoch')!=epoch:
                        cursor['recheck_epoch']=epoch;cursor['recheck_after']=''
                    with self._transaction(context) as c:
                        rows=c.execute('SELECT '+self._projection()+''',e.learning_eligible AS episode_eligible,
                            e.payload->'learning_integrity' AS learning_integrity,
                            e.payload->>'learning_exclusion_reason' AS learning_exclusion_reason,'''+
                            LI.evidence_hash_sql('t')+''' AS learning_evidence_hash,
                            r.trade_id AS receipt_trade_id,r.observation AS original_observation,
                            r.evidence_hash AS original_evidence_hash
                            FROM learning_trade_receipts r LEFT JOIN paper_trades t ON t.trade_id=r.trade_id
                            LEFT JOIN v90_learning_episodes e ON e.trade_id=t.trade_id
                            WHERE r.valid=TRUE AND r.trade_id>%s ORDER BY r.trade_id LIMIT %s''',
                            (cursor.get('recheck_after',''),BATCH_SIZE)).fetchall()
                    revoked=[];ids=[];clock=datetime.now(timezone.utc)
                    for raw in rows:
                        self._check(context)
                        item,reason=observation(dict(raw),now=clock)
                        if (item is None or item.get('evidence_valid') is not True
                                or item.get('evidence_hash')!=raw['original_evidence_hash']):
                            original=dict(raw['original_observation'])
                            original.update(evidence_valid=False,observed_at=clock.isoformat())
                            revoked.append(original);ids.append(raw['receipt_trade_id'])
                    if revoked:
                        # Admission/risk fields are also immutable evidence,
                        # even when the LI diagnostic projection is unchanged.
                        LI.invalidate('consumed_trade_evidence_changed')
                        published_snapshot=AUTO.run_batch(pg,revoked,now=clock,context=context)
                        with self._transaction(context) as c:
                            c.execute('UPDATE learning_trade_receipts SET valid=FALSE,updated_at=now() WHERE trade_id=ANY(%s)',(ids,))
                    current=LI.memory_state()
                    if not rows and current['ready'] and [current['process_epoch'],current['revocation_generation']]==epoch:
                        self._verified_epoch=epoch
                        published_snapshot=published_snapshot or AUTO.snapshot(pg)
                        cursor['recheck_after']=''
                    elif rows:cursor['recheck_after']=rows[-1]['receipt_trade_id']
                    result.update(checked=len(rows),revoked=len(revoked))
                    # Drain the bounded receipt sweep without waiting for
                    # three unrelated phases between each four-row page.
                    cursor['phase']='recheck' if rows else 'materialize'
            else:
                raise ValueError('unknown closed-trade learning phase')
            self._check(context)
            if not STORE.checkpoint_job(pg,lease,status='OK',cursor=cursor,result=result):
                raise RuntimeError('learning job lease expired before progress commit')
            if published_snapshot is not None:
                result['snapshot']=dict(published_snapshot,**self.validation_status())
            return result
        except Exception as ex:
            # Report the type without leaking a connection string/query payload.
            STORE.checkpoint_job(pg,lease,status='ERROR',result={'error_type':type(ex).__name__},retry_after_seconds=15)
            raise

    def validation_status(self):
        state=LI.memory_state()
        ready=state['ready'] and self._verified_epoch==[state['process_epoch'],state['revocation_generation']]
        return {'evidence_revalidation_pending':not ready,
                'verified_generation':state['generation'] if ready else None,
                'verified_revocation_generation':self._verified_epoch[1] if ready else None,
                'verified_process_epoch':self._verified_epoch[0] if ready else None}

    def refresh_memory(self,context=None):
        """Rebuild a small verified snapshot; never revive a stored process token."""
        self._check(context)
        token=LI.generation()
        if not LI.memory_state()['ready']:
            return {'status':'NO_WORK','reason':'AWAIT_COMMITTED_VALIDATION'}
        with self._transaction(context) as c:
            rows=c.execute("""SELECT e.trade_id,e.asset,e.horizon,e.direction,e.closed_at,e.net_pnl_rub,
                t.payload->>'entry_nav_rub' AS entry_nav_rub,
                t.payload#>>'{setup_memory,setup_family}' AS family,
                t.payload#>>'{setup_memory,regime_bucket}' AS regime,
                t.payload#>>'{setup_memory,entry_state}' AS entry_state
                FROM v90_learning_episodes e JOIN paper_trades t ON t.trade_id=e.trade_id
                WHERE """+LI.readable_sql('e')+"""
                ORDER BY e.closed_at DESC,e.trade_id LIMIT 64""").fetchall()
        clock=datetime.now(timezone.utc)
        board=memory_board(rows,token,now=clock,half_life_days=self.ns.get('EXPERIENCE_DECAY_HALF_LIFE_DAYS',45.),
            thresholds={'size':self.ns.get('EXPERIENCE_MIN_SIZE_N',8),'execution':self.ns.get('EXPERIENCE_MIN_EXEC_N',20),
                        'weight':self.ns.get('EXPERIENCE_MIN_WEIGHT_N',40)})
        self._check(context)
        if not EXPORTS.memory_current(board):
            return {'status':'NO_WORK','reason':'EVIDENCE_CHANGED_DURING_BUILD'}
        if not STORE.publish_snapshot(self.ns['pg_connect'],'verified_setup_memory',VERSION,board,observed_at=clock):
            raise RuntimeError('verified memory publication rejected')
        self._check(context)
        if not EXPORTS.memory_current(board):
            return {'status':'NO_WORK','reason':'EVIDENCE_CHANGED_DURING_PUBLISH'}
        builder=self.ns.get('setup_memory_board')
        if builder is None:raise RuntimeError('setup memory consumer is unavailable')
        builder._cache=(time.time(),board)
        lookup=self.ns.get('_v842_memory_lookup')
        if lookup is not None:lookup._cache=None
        return {'status':'OK','verified_episodes':len(rows),'memory_items':len(board['items']),
                'window_limit':64,'evidence_generation':token}


def memory_board(rows, generation, *, now=None, half_life_days=45., thresholds=None):
    """The existing hierarchical Beta(5,5) reducer over original entry context.

    Only known error labels may train error rates. Clean eligible outcomes do
    not claim hypothetical alternate stops or profitable rejected entries.
    """
    clock=_time(now) if now is not None else datetime.now(timezone.utc)
    half=_number(half_life_days)
    if not clock or half is None or half<=0:raise ValueError('invalid memory decay')
    thresholds=thresholds or {'size':8,'execution':20,'weight':40}
    if any(_number(thresholds.get(k)) is None or thresholds[k]<=0 for k in ('size','execution','weight')):
        raise ValueError('invalid memory thresholds')
    buckets={};seen=set();used=0
    rows=list(rows)
    if len(rows)>64:raise ValueError('verified memory batch exceeds 64')
    for raw in rows:
        r=dict(raw);tid=r.get('trade_id')
        if not tid or tid in seen:continue
        seen.add(tid)
        net,nav=_number(r.get('net_pnl_rub')),_number(r.get('entry_nav_rub'))
        closed=_time(r.get('closed_at'))
        if (net is None or nav is None or nav<=0 or not closed or closed>clock
                or not all(isinstance(r.get(k),str) and r[k] for k in ('family','regime','entry_state','asset','horizon'))
                or r.get('direction') not in ('LONG','SHORT')):continue
        age=(clock-closed).total_seconds()/86400.
        weight=.20*math.exp(-math.log(2.)*age/half)
        family,regime,entry=r['family'],r['regime'],r['entry_state']
        asset,h,d=r['asset'],r['horizon'],r['direction']
        for key in (('EXACT',asset,h,family,d,regime,entry),('ASSET_FAMILY',asset,h,family,d,'*','*'),
                    ('REGIME_FAMILY','*',h,family,d,regime,'*'),('FAMILY','*',h,family,d,'*','*')):
            b=buckets.setdefault(key,{'n':0,'w':0.,'wins':0.,'pnl':0.})
            b['n']+=1;b['w']+=weight;b['wins']+=weight if net>0 else 0.;b['pnl']+=weight*net/nav
        used+=1
    items=[]
    for key,z in buckets.items():
        level,asset,h,fam,d,reg,ent=key;weight=z['w']
        items.append(dict(level=level,asset=asset,horizon=h,setup_family=fam,direction=d,
            regime_bucket=reg,entry_state=ent,n=z['n'],effective_n=round(weight,2),
            posterior_win_rate=round((z['wins']+5.)/(weight+10.),4),
            weighted_avg_pnl=round(z['pnl']/weight,6) if weight else None,
            stop_error_rate=0.,direction_error_rate=0.,late_entry_error_rate=0.,
            good_execution_rate=0.,rejected_n=0,rejected_winner_rate=None,
            status='WEIGHT_READY' if weight>=thresholds['weight'] else 'EXECUTION_READY' if weight>=thresholds['execution'] else 'SIZE_READY' if weight>=thresholds['size'] else 'BUILDING'))
    items.sort(key=lambda r:(r['level']!='EXACT',-r['effective_n'],r['asset'],r['horizon']))
    return dict(status='OK',items=items,**EXPORTS.memory_contract(generation),
        verified_episodes=used,window_limit=64,learning_weight=.20,
        thresholds=dict(thresholds),
        hierarchy=['EXACT','ASSET_FAMILY','REGIME_FAMILY','FAMILY'],
        principle='Original verified paper outcomes; no reconstructed entry context or hypothetical stop labels')
