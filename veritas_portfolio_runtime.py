"""Canonical VERITAS portfolio runtime layers R42-R46.
Separated from veritas_portfolio.py to keep the base engine stable and prevent
further monkey-patch accumulation in the core module. The runtime module is
loaded only after the R41 base has finished initializing.
"""
import veritas_portfolio as _vp_base
import veritas_trend_entry as VTE
import veritas_launch_readiness as VLR
import veritas_canonical_constitution as CTC
import veritas_canonical_runtime as VCR
import veritas_timeframe_policy as TFP
import veritas_release as VR
import veritas_profit_maturity as VPM
import veritas_peer_invalidation as VPI
import veritas_episode_profit_floor as VEF
_BASE = {k: v for k, v in vars(_vp_base).items() if not k.startswith('__')}
globals().update(_BASE)
# VERITAS V90 CANONICAL EXECUTION KERNEL R42
# One authoritative paper-admission path. Historical R16/Q2/R19/R40/R41
# layers remain in the file for migration/audit history, but are no longer
# allowed to veto a directional setup after the hard safety domains pass.
#
# Invariant:
#   signal -> source/time -> final economics -> hard setup/risk -> sizing -> order
# Soft model-quality / probability information changes size, not the existence
# of a research-paper probe. Live capital remains independently fail-closed in
# veritas_live + veritas_execution.production_order_gate.
V90_PRODUCTION_CANDIDATE_EPOCH=os.getenv(
    'VERITAS_PRODUCTION_CANDIDATE_EPOCH','2026-09-29T18:42:15+00:00'
)
V90_PRODUCTION_CANDIDATES=('Champion','Challenger')
def _v90_candidate_profit_guard(row,policy,economics):
    """Profitability-first gate for the production-candidate paper books.

    Research signal generation stays broad. The candidate books trade only
    confirmed, cost-efficient setups and stop repeating empirically weak ones.
    """
    row=row or {}; policy=policy or {}; economics=economics or {}
    mode=str(policy.get('mode') or 'CORE')
    plan=row.get('trade_plan') or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    hs=row.get('horizon_structure') or {}
    memory=plan.get('setup_memory') or {}
    state=str(bq.get('state') or '')
    blockers=[]

    try: indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception: indep=0
    supporting=list(row.get('_supporting_horizons') or [])
    alignment=int(row.get('_alignment_count') or len(set(supporting)))
    try: hscore=float(hs.get('score') or 0.0)
    except Exception: hscore=0.0
    try: rr=float(economics.get('expected_to_stop_ratio') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try: expected=abs(float(economics.get('expected_move_pct') or plan.get('expected_move_pct') or 0.0))
    except Exception: expected=0.0
    try: cost=float(economics.get('modeled_round_trip_cost_pct') or 0.0)
    except Exception: cost=0.0
    cost_to_edge=(cost/expected) if expected>0 else 999.0

    # Loss audit: weak breakouts repeatedly generated zero-win clusters.
    if state=='WEAK_BREAKOUT':
        blockers.append('WEAK_BREAKOUT_NEGATIVE_HISTORY')

    # Never spend most of the expected move on friction.
    max_cost_ratio=0.30 if mode in ('IMPULSE_ONLY','AGGRESSIVE') else 0.25
    if expected<=0 or cost_to_edge>max_cost_ratio:
        blockers.append('COST_TO_EDGE_TOO_HIGH')

    # Champion and Challenger are the production-candidate books.
    if mode in ('CORE','CHALLENGER'):
        need_indep=3 if mode=='CORE' else 4
        need_align=2 if mode=='CORE' else 3
        need_hscore=0.60 if mode=='CORE' else 0.64
        need_rr=1.35 if mode=='CORE' else 1.50
        if indep<need_indep:
            blockers.append('INSUFFICIENT_INDEPENDENT_EVIDENCE')
        if alignment<need_align:
            blockers.append('INSUFFICIENT_MULTI_TF_ALIGNMENT')
        if hscore<need_hscore:
            blockers.append('HORIZON_STRUCTURE_TOO_WEAK')
        if rr<need_rr:
            blockers.append('NET_REWARD_RISK_TOO_LOW')

        entry_quality=str(plan.get('entry_quality') or row.get('entry_quality') or '')
        rebased=bool(plan.get('entry_quality_rebased_from_old_setup'))
        if entry_quality=='INVALIDATED' and not rebased:
            blockers.append('ENTRY_QUALITY_INVALIDATED')

        # Early breakouts may be researched, but production candidates wait for
        # substantially stronger confirmation.
        if state=='EARLY_BREAKOUT':
            if not (indep>=4 and alignment>=3 and hscore>=0.68
                    and rr>=1.60 and expected>=max(0.006,3.0*cost)):
                blockers.append('EARLY_BREAKOUT_WAIT_CONFIRMATION')

        # Do not keep repeating a setup once durable experience says its
        # realized economics are negative.
        try: mem_n=float(memory.get('effective_n') or 0.0)
        except Exception: mem_n=0.0
        mem_p=memory.get('posterior_win_rate')
        mem_pnl=memory.get('weighted_avg_pnl')
        if mem_n>=8:
            try:
                if mem_pnl is not None and float(mem_pnl)<=0:
                    blockers.append('NEGATIVE_SETUP_EXPECTANCY_HISTORY')
            except Exception:
                pass
            try:
                if mem_p is not None and float(mem_p)<0.48:
                    blockers.append('SETUP_WIN_RATE_TOO_LOW')
            except Exception:
                pass
        if mem_n>=20:
            try:
                if mem_p is not None and float(mem_p)<0.55:
                    blockers.append('MATURE_SETUP_WIN_RATE_TOO_LOW')
            except Exception:
                pass

        analog_n=row.get('analog_effective_n')
        analog_p=row.get('positive_trade_probability')
        try:
            if analog_n is not None and float(analog_n)>=8 and analog_p is not None and float(analog_p)<0.50:
                blockers.append('NEGATIVE_ANALOG_EDGE')
        except Exception:
            pass

    return {
        'eligible':not blockers,
        'status':'PASS' if not blockers else 'BLOCK',
        'blockers':list(dict.fromkeys(blockers)),
        'independent':indep,'alignment_count':alignment,
        'horizon_structure_score':hscore,
        'net_reward_risk':rr,'expected_move_pct':expected,
        'modeled_round_trip_cost_pct':cost,
        'cost_to_edge_ratio':cost_to_edge,
        'breakout_state':state,
        'setup_memory_effective_n':memory.get('effective_n'),
        'setup_memory_win_rate':memory.get('posterior_win_rate'),
        'setup_memory_avg_pnl':memory.get('weighted_avg_pnl'),
    }

def _v90_canonical_quality_admission(row, policy, drawdown):
    row = row or {}
    policy = policy or {}
    direction = str(row.get('research_decision') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return {
            'open':False,'fraction':0.0,'reason':'CANONICAL_NO_DIRECTION',
            'probability':None,'model_quality_score':None,
            'probability_source':None,
        }

    if not _v901_no_hard_veto(row):
        score, source = _signal_probability(row)
        empirical = source == 'EMPIRICAL_CALIBRATION'
        return {
            'open':False,'fraction':0.0,'reason':'CANONICAL_HARD_VETO',
            'probability':float(score) if empirical else None,
            'model_quality_score':None if empirical else float(score),
            'probability_source':source,
        }

    rg = _risk_governor(drawdown)
    if rg.get('new_risk') is False:
        score, source = _signal_probability(row)
        empirical = source == 'EMPIRICAL_CALIBRATION'
        return {
            'open':False,'fraction':0.0,'reason':'CANONICAL_RISK_GOVERNOR_HARD',
            'probability':float(score) if empirical else None,
            'model_quality_score':None if empirical else float(score),
            'probability_source':source,'risk_governor':rg,
        }

    mode = str(policy.get('mode') or 'CORE')
    plan = row.get('trade_plan') or {}
    hs = row.get('horizon_structure') or {}
    inst = row.get('institutional_signal') or {}
    score, source = _signal_probability(row)
    empirical = source == 'EMPIRICAL_CALIBRATION'
    economics=row.get('_canonical_economics_gate') or {}

    guard=_v90_candidate_profit_guard(row,policy,economics)
    if not guard.get('eligible'):
        return {
            'open':False,'fraction':0.0,'reason':'PROFITABILITY_GATE',
            'profitability_blockers':guard.get('blockers') or [],
            'profitability_gate':guard,
            'probability':float(score) if empirical else None,
            'model_quality_score':None if empirical else round(float(score),6),
            'probability_source':source,
        }

    try:
        rr = float(guard.get('net_reward_risk') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception:
        rr = 0.0
    try:
        hscore = float(hs.get('score') or 0.0)
    except Exception:
        hscore = 0.0
    try:
        independent = int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)
    except Exception:
        independent = 0

    supporting = list(row.get('_supporting_horizons') or [])
    alignment = int(row.get('_alignment_count') or len(set(supporting)))
    tier = str(row.get('signal_tier') or row.get('execution_signal_tier') or '')

    # Research books explore; production candidates start smaller and earn size.
    base = {
        'IMPULSE_ONLY':0.10,
        'AGGRESSIVE':0.15,
        'CORE':0.05,
        'CHALLENGER':0.05,
    }.get(mode,0.05)
    f = base
    quality = float(score)

    if rr >= 1.40 and independent >= 3 and hscore >= 0.60:
        f = max(f, {
            'IMPULSE_ONLY':0.15,'AGGRESSIVE':0.25,
            'CORE':0.10,'CHALLENGER':0.10,
        }.get(mode,0.10))
    if alignment >= 3 and rr >= 1.60 and independent >= 4 and hscore >= 0.68:
        f = max(f, {
            'IMPULSE_ONLY':0.25,'AGGRESSIVE':0.40,
            'CORE':0.15,'CHALLENGER':0.15,
        }.get(mode,0.15))
    if alignment >= 4 and rr >= 1.80 and independent >= 4 and hscore >= 0.74:
        f = max(f, {
            'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.75,
            'CORE':0.25,'CHALLENGER':0.20,
        }.get(mode,0.20))

    threshold = float(policy.get('threshold') or 0.0)
    strong_threshold = float(policy.get('strong_threshold') or 1.0)
    if empirical and quality >= threshold:
        f = max(f, {
            'IMPULSE_ONLY':0.20,'AGGRESSIVE':0.30,
            'CORE':0.10,'CHALLENGER':0.10,
        }.get(mode,0.10))
    if empirical and quality >= strong_threshold and tier in ('SUPER_LONG','SUPER_SHORT'):
        f = max(f, {
            'IMPULSE_ONLY':0.50,'AGGRESSIVE':1.00,
            'CORE':0.40,'CHALLENGER':0.30,
        }.get(mode,0.25))

    if mode == 'AGGRESSIVE':
        strong_f = _v90_aggressive_strong_fraction(row, policy, drawdown)
        if strong_f is not None:
            f = max(f, float(strong_f))

    # Original execution rule: an early entry is a small probe, then add only
    # after confirmation. Never let score/portfolio aggressiveness turn
    # EARLY_BREAKOUT directly into a large initial position.
    if str(guard.get('breakout_state') or '')=='EARLY_BREAKOUT':
        f=min(f,0.05)

    if not empirical:
        f = min(f, {
            'IMPULSE_ONLY':0.35,'AGGRESSIVE':0.75,
            'CORE':0.25,'CHALLENGER':0.20,
        }.get(mode,0.20))

    memory=plan.get('setup_memory') or {}
    try:
        mem_n=float(memory.get('effective_n') or 0.0)
        mem_p=float(memory.get('posterior_win_rate')) if memory.get('posterior_win_rate') is not None else None
        mem_pnl=float(memory.get('weighted_avg_pnl')) if memory.get('weighted_avg_pnl') is not None else None
        if mem_n>=8 and mem_p is not None and mem_p>=0.65 and (mem_pnl is None or mem_pnl>0):
            f*=1.15
    except Exception:
        pass

    # R47 bounded exploration: soft learning warnings can only REDUCE an
    # otherwise valid research trade. They cannot bypass final economics.
    try:
        f*=float(guard.get('size_multiplier') or 1.0)
    except Exception:
        pass
    try:
        if guard.get('size_cap') is not None:
            f=min(f,float(guard.get('size_cap')))
    except Exception:
        pass

    # R50: three-stage 5m sizing. The first 5m trade is evidence collection,
    # not full-size authority. Historical independent episodes showed that large
    # first allocations (>20%) concentrated most losses, while <=10% was far
    # less damaging. Earn size only with empirical calibration and senior-TF
    # confirmation; use 1h+ to authorize material scale-up.
    five_minute_policy=None
    if str(row.get('horizon') or '')=='5m':
        senior_support=any(
            str(h) in ('1h','4h','1d','3d','7d') for h in supporting
        )
        breakout_state=str(guard.get('breakout_state') or '')
        empirical_probe=bool(
            empirical
            and quality>=threshold
            and independent>=3
            and alignment>=2
            and hscore>=0.62
            and rr>=1.35
            and senior_support
            and breakout_state not in ('WEAK_BREAKOUT',)
        )
        mature_5m=bool(
            empirical_probe
            and quality>=strong_threshold
            and independent>=4
            and alignment>=4
            and hscore>=0.74
            and rr>=1.80
            and breakout_state not in ('EARLY_BREAKOUT','WEAK_BREAKOUT')
        )
        if mature_5m:
            cap={
              'IMPULSE_ONLY':0.15,'AGGRESSIVE':0.25,
              'CORE':0.10,'CHALLENGER':0.10,
            }.get(mode,0.10)
            five_minute_policy='MATURE_EMPIRICAL_SENIOR_CONFIRMED'
        elif empirical_probe:
            cap={
              'IMPULSE_ONLY':0.10,'AGGRESSIVE':0.10,
              'CORE':0.05,'CHALLENGER':0.05,
            }.get(mode,0.05)
            five_minute_policy='EMPIRICAL_DISCOVERY_PROBE'
        else:
            cap=0.05
            five_minute_policy='UNCALIBRATED_DISCOVERY_PROBE'
        f=min(f,cap)
        row['_five_minute_sizing_policy']=five_minute_policy
        row['_five_minute_size_cap']=cap

    risk_pct = plan.get('stop_distance_pct')
    if risk_pct is None:
        risk_pct = (inst or {}).get('risk_pct')
    try:
        rp = float(risk_pct or 0.0)
        if rp > 0:
            f = min(f, MAX_STOP_RISK_NAV / rp)
    except Exception:
        pass

    f *= float(rg.get('multiplier') or 0.0)
    max_fraction = float(policy.get('max_fraction') or 2.0)
    f = _clip(_round_step(f), 0.0, max_fraction)

    return {
        'open':f > 0.0,
        'fraction':f,
        'reason':'CANONICAL_SIGNAL_ENTRY',
        'probability':float(score) if empirical else None,
        'model_quality_score':None if empirical else round(float(score),6),
        'probability_source':source,
        'empirical':empirical,
        'rr':rr,
        'alignment_count':alignment,
        'independent_evidence':independent,
        'horizon_structure_score':hscore,
        'profitability_gate':guard,
        'risk_governor':rg,
        'sizing_authority':'PROFITABILITY_FIRST_SIGNAL_THEN_RISK',
        'five_minute_sizing_policy':five_minute_policy,
        'five_minute_size_cap':row.get('_five_minute_size_cap'),
        'legacy_soft_gates_authoritative':False,
    }

# Keep this alias injectable for existing regression tests, while replacing
# the historical nested chain with the canonical quality/sizing authority.
_v90r41_base_admission = _v90_canonical_quality_admission

def _signal_first_admission(row, policy, drawdown):
    row = row or {}
    asset = str(row.get('asset') or '')

    # Paper uses one valid primary source. An explicit retained denial remains
    # authoritative; missing router fields are reconstructed from the source gate.
    source_gate = VX.paper_source_gate(asset, row) if asset in VX.PAPER_ASSETS else {
        'eligible':bool(row.get('execution_eligible')),
        'reason':row.get('paper_execution_reason') or row.get('execution_reason'),
        'blockers':[],
    }
    explicit_paper = row.get('paper_eligible')
    paper_ok = bool(source_gate.get('eligible')) and explicit_paper is not False
    row['paper_eligible'] = paper_ok
    row['paper_execution_reason'] = (
        source_gate.get('reason') if paper_ok
        else source_gate.get('reason') if not source_gate.get('eligible')
        else 'paper_explicit_denial'
    )
    if not paper_ok:
        return {
            'open':False,'fraction':0.0,'reason':'R42_PAPER_SOURCE_GATE',
            'source_blockers':source_gate.get('blockers') or [],
            'execution_reason':row.get('execution_reason'),
            'paper_execution_reason':row.get('paper_execution_reason'),
            'production_eligible':bool(row.get('production_eligible')),
            'research_signal_preserved':True,
        }

    direction = str(row.get('research_decision') or 'NO_TRADE')
    if direction not in ('LONG','SHORT'):
        return {
            'open':False,'fraction':0.0,'reason':'CANONICAL_NO_DIRECTION',
            'paper_source_quality':'PRODUCTION_GRADE' if row.get('production_eligible') else 'RESEARCH_GRADE',
            'paper_is_live_fill_evidence':False,
        }

    plan = row.get('trade_plan') or {}
    economics = VX.entry_gate(
        row, row.get('price'), direction,
        plan.get('initial_position_fraction', 0.10)
    )
    if economics.get('status') == 'BLOCK':
        return {
            'open':False,'fraction':0.0,'reason':'R41_FINAL_ECONOMICS_GATE',
            'economics_blockers':economics.get('blockers') or [],
            'quote_time_gate':economics.get('quote_time_gate'),
            'modeled_round_trip_cost_pct':economics.get('modeled_round_trip_cost_pct'),
            'minimum_expected_move_pct':economics.get('minimum_expected_move_pct'),
            'net_reward_risk':economics.get('expected_to_stop_ratio'),
            'research_signal_preserved':True,
            'paper_source_quality':'PRODUCTION_GRADE' if row.get('production_eligible') else 'RESEARCH_GRADE',
            'paper_is_live_fill_evidence':False,
        }

    row['_canonical_economics_gate']=economics
    out = dict(_v90r41_base_admission(row, policy, drawdown) or {})
    out['paper_source_quality'] = 'PRODUCTION_GRADE' if row.get('production_eligible') else 'RESEARCH_GRADE'
    out['paper_is_live_fill_evidence'] = False
    out['economics_gate'] = 'PASS'
    out['modeled_round_trip_cost_pct'] = economics.get('modeled_round_trip_cost_pct')
    out['net_reward_risk'] = economics.get('expected_to_stop_ratio')
    out['production_eligible'] = bool(row.get('production_eligible'))
    return out

def _desired_fraction(row, policy, drawdown):
    admission = _signal_first_admission(row, policy, drawdown)
    if not admission.get('open'):
        return 0.0

    # Impulse has its own candidate book; retain its timeframe/setup boundary.
    if str((policy or {}).get('mode') or '') == 'IMPULSE_ONLY':
        h = str((row or {}).get('horizon') or '')
        allowed = tuple((policy or {}).get('allowed_horizons') or ('5m','1h','4h','1d'))
        if h not in allowed:
            return 0.0
        if not (
            (row or {}).get('_impulse_setup')
            or ((row or {}).get('impulse_genesis') or {}).get('active')
            or ((row or {}).get('impulse_pivot_break') or {}).get('active')
            or ((row or {}).get('tactical_reversal') or {}).get('active')
            or ((row or {}).get('range_retest_breakout') or {}).get('active')
        ):
            return 0.0
    return float(admission.get('fraction') or 0.0)

def _portfolio_admission_trace(candidates, policy, drawdown):
    return VAT.build(candidates, _portfolio_canonical_setup_id)

_v90_candidate_base_step_one=_step_one

def _v90_candidate_epoch_rebase(c,name,prices,ts):
    # One-time hygiene for ALL four books: positions opened by the superseded
    # admission kernel must not keep generating P/L after the clean test epoch.
    # Historical trades/P&L remain untouched; only still-open legacy exposure closes.
    epoch_key=re.sub(r'[^0-9A-Za-z]+','_',str(V90_PRODUCTION_CANDIDATE_EPOCH)).strip('_')[-48:]
    marker='execution_epoch_cleanup_'+epoch_key+'_'+str(name)
    try:
        row=c.execute("SELECT 1 AS ok FROM v90_migration_state WHERE key=%s",(marker,)).fetchone()
        if row:
            return {'status':'ALREADY_REBASED','closed':0}
        epoch=datetime.fromisoformat(str(V90_PRODUCTION_CANDIDATE_EPOCH).replace('Z','+00:00'))
        if epoch.tzinfo is None:
            epoch=epoch.replace(tzinfo=timezone.utc)
        p,pos=_portfolio_rows(c,name)
        nav,_,_,_=_mark_nav(p,pos,prices)
        closed=0
        kept=0
        for z0 in list(pos):
            z=dict(z0)
            opened=z.get('opened_at')
            try:
                odt=opened if isinstance(opened,datetime) else datetime.fromisoformat(str(opened).replace('Z','+00:00'))
                if odt.tzinfo is None:
                    odt=odt.replace(tzinfo=timezone.utc)
            except Exception:
                kept+=1
                continue
            if odt>=epoch:
                kept+=1
                continue
            px=float((prices or {}).get(z.get('asset'),z.get('last_price')))
            _close_or_reduce(c,p,name,z,px,0.0,nav,ts,'LEGACY_KERNEL_REBASE')
            closed+=1
        c.execute("""INSERT INTO v90_migration_state(key,migrated_at,details)
                     VALUES(%s,now(),%s::jsonb) ON CONFLICT(key) DO NOTHING""",
                  (marker,json.dumps({'portfolio':name,'epoch':V90_PRODUCTION_CANDIDATE_EPOCH,
                                      'closed_pre_epoch_positions':closed,
                                      'kept_current_epoch_positions':kept})))
        if closed:
            print(json.dumps({'event':'V90_LEGACY_KERNEL_REBASE','portfolio':name,
                              'epoch':V90_PRODUCTION_CANDIDATE_EPOCH,
                              'closed':closed,'kept':kept},
                             ensure_ascii=False,default=str,separators=(',',':')),flush=True)
        return {'status':'REBASED','closed':closed,'kept':kept}
    except Exception as ex:
        return {'status':'ERROR','closed':0,'error':f'{type(ex).__name__}: {ex}'}

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    _v90_candidate_epoch_rebase(c,name,prices,ts)
    return _v90_candidate_base_step_one(
        c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary
    )

def _v90_candidate_metrics(pg_connect,name):
    with pg_connect() as c:
        r=c.execute("""
          SELECT COUNT(*) AS closed_trades,
                 COUNT(DISTINCT COALESCE(payload->>'canonical_setup_id',trade_id)) AS unique_episodes,
                 COUNT(*) FILTER(WHERE net_pnl_rub>0) AS wins,
                 COALESCE(SUM(net_pnl_rub),0) AS net_pnl_rub,
                 COALESCE(AVG(net_pnl_rub),0) AS avg_net_pnl_rub,
                 COALESCE(SUM(CASE WHEN net_pnl_rub>0 THEN net_pnl_rub ELSE 0 END),0) AS gross_wins_rub,
                 ABS(COALESCE(SUM(CASE WHEN net_pnl_rub<0 THEN net_pnl_rub ELSE 0 END),0)) AS gross_losses_rub,
                 COALESCE(SUM(fees_rub+funding_rub),0) AS costs_rub,
                 COUNT(*) FILTER(WHERE COALESCE(payload->>'exit_reason',payload->>'close_reason','') IN ('','UNKNOWN')) AS unknown_exits
          FROM paper_trades
          WHERE portfolio_name=%s
            AND opened_at >= %s::timestamptz
            AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
        """,(name,V90_PRODUCTION_CANDIDATE_EPOCH)).fetchone()
        dd=c.execute("""
          WITH x AS (
            SELECT observed_at,nav_rub,
                   MAX(nav_rub) OVER (ORDER BY observed_at ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW) AS hwm
            FROM paper_nav_history
            WHERE portfolio_name=%s AND observed_at >= %s::timestamptz
          )
          SELECT COALESCE(MAX(CASE WHEN hwm>0 THEN (hwm-nav_rub)/hwm ELSE 0 END),0) AS max_drawdown
          FROM x
        """,(name,V90_PRODUCTION_CANDIDATE_EPOCH)).fetchone()
    n=int((r or {}).get('closed_trades') or 0)
    wins=int((r or {}).get('wins') or 0)
    gw=float((r or {}).get('gross_wins_rub') or 0.0)
    gl=float((r or {}).get('gross_losses_rub') or 0.0)
    pf=(gw/gl) if gl>0 else (999.0 if gw>0 else None)
    return {
      'portfolio':name,
      'closed_trades':n,
      'unique_episodes':int((r or {}).get('unique_episodes') or 0),
      'wins':wins,
      'win_rate':wins/n if n else None,
      'net_pnl_rub':float((r or {}).get('net_pnl_rub') or 0.0),
      'avg_net_pnl_rub':float((r or {}).get('avg_net_pnl_rub') or 0.0),
      'profit_factor':pf,
      'costs_rub':float((r or {}).get('costs_rub') or 0.0),
      'unknown_exits':int((r or {}).get('unknown_exits') or 0),
      'max_drawdown':float((dd or {}).get('max_drawdown') or 0.0),
    }

def production_candidate_readiness(pg_connect):
    thresholds={
      'min_closed_trades':50,
      'min_unique_episodes':50,
      'min_win_rate':0.65,
      'min_profit_factor':1.25,
      'max_drawdown':0.10,
      'require_positive_net_pnl':True,
      'require_positive_avg_trade':True,
      'unknown_exit_tolerance':0,
    }
    try:
        champion=_v90_candidate_metrics(pg_connect,'Champion')
        challenger=_v90_candidate_metrics(pg_connect,'Challenger')
    except Exception as ex:
        return {'status':'UNAVAILABLE','ready':False,'epoch':V90_PRODUCTION_CANDIDATE_EPOCH,
                'error':f'{type(ex).__name__}: {ex}','thresholds':thresholds}

    def evaluate(m):
        checks={
          'sample':int(m.get('closed_trades') or 0)>=thresholds['min_closed_trades'],
          'independent_sample':int(m.get('unique_episodes') or 0)>=thresholds['min_unique_episodes'],
          'win_rate':m.get('win_rate') is not None and float(m['win_rate'])>=thresholds['min_win_rate'],
          'profit_factor':(m.get('profit_factor') is not None and float(m['profit_factor'])>=thresholds['min_profit_factor'])
              or m.get('profit_factor_state')=='NO_LOSSES',
          'positive_net_pnl':float(m.get('net_pnl_rub') or 0.0)>0,
          'positive_avg_trade':float(m.get('avg_net_pnl_rub') or 0.0)>0,
          'drawdown':m.get('max_drawdown') is not None and float(m['max_drawdown'])<=thresholds['max_drawdown'],
          'history_coverage':bool(m.get('history_complete')),
          'rule_evidence':bool(m.get('rule_evidence_complete')),
          'accounting':bool(m.get('accounting_complete')),
          'exit_telemetry':int(m.get('unknown_exits') or 0)<=thresholds['unknown_exit_tolerance'],
        }
        return checks,all(checks.values())
    cc,cr=evaluate(champion); hc,hr=evaluate(challenger)
    champion['checks']=cc; champion['ready']=cr
    challenger['checks']=hc; challenger['ready']=hr
    return {
      'status':'PASS' if cr else 'BUILDING',
      'ready':cr,
      'primary_candidate':'Champion',
      'challenger_required_for_live':False,
      'epoch':V90_PRODUCTION_CANDIDATE_EPOCH,
      'thresholds':thresholds,
      'roles':{
        'Impulse':'research / impulse discovery',
        'Aggressive':'research / high-risk discovery',
        'Champion':'primary production candidate',
        'Challenger':'strict alternative candidate',
      },
      'Champion':champion,'Challenger':challenger,
      'profitability_guaranteed':False,
      'principle':'No real-money promotion until the primary paper candidate proves positive post-cost performance on a fresh independent execution epoch.',
    }

_v90_canonical_report_base = report

def report(pg_connect):
    d = dict(_v90_canonical_report_base(pg_connect) or {})
    d['canonical_execution_kernel'] = {
        'active':True,
        'version':'v9.0',
        'path':'signal -> source/time -> economics -> hard setup/risk -> sizing -> order',
        'legacy_soft_gates_authoritative':False,
        'starter_position_on_valid_directional_signal':True,
        'quality_controls_size_not_signal_existence':True,
        'profitability_first_candidate_books':['Champion','Challenger'],
        'weak_breakout_trading_enabled':False,
        'profit_protection_trigger':'max(0.25%, round-trip commission + 0.12%)',
        'profit_floor_after_trigger':'round-trip commission + 0.03%',
        'pre_epoch_open_positions_allowed':False,
        'candidate_epoch':V90_PRODUCTION_CANDIDATE_EPOCH,
        'live_capital_gate_independent':True,
        'live_execution_armed':False,
        'principle':'Paper gathers evidence; real capital stays fail-closed until broker, direct data, instrument, promotion and risk gates pass.',
    }
    d['production_candidate_readiness']=production_candidate_readiness(pg_connect)
    return _jsonable(d)

# VERITAS V90 CLOSED-LOOP LEARNING BRIDGE R43
# Fixes the final R42 feedback gap found in the closed-trade audit.
#
# R29/R33 already learn from completed clean episodes, but R42 introduced a
# canonical admission path that did not fully consume their learned edge
# calibration and context sizing. R43 reconnects those layers without bringing
# back the historical nested soft-veto chain.
#
# Principles:
# - learning comes only from completed, integrity-clean episodes;
# - exit/stop-management errors never punish directional quality by themselves;
# - expected-move overforecast recalibrates reward/risk before sizing;
# - repeated entry-direction/cost failures can veto Champion/Challenger context;
# - research books remain able to explore, but learned weakness reduces size;
# - positive learning remains bounded and cannot bypass hard source/risk/economics gates.

V90_R43_STARTED_AT=os.getenv(
    'VERITAS_R43_STARTED_AT','2026-09-29T19:54:00+00:00'
)

_v90r43_base_candidate_guard=_v90_candidate_profit_guard
_v90r43_base_admission=_signal_first_admission
_v90r43_base_report=report

def _v90r43_learning_edge(row,guard=None):
    row=row or {}
    guard=guard or {}

    episodes=int(_v90r33_cache.get('n') or 0)
    raw_global_haircut=1.0
    if episodes>=10:
        try:
            raw_global_haircut=_clip(float(_v90r33_cache.get('edge_haircut') or 1.0),0.60,1.0)
        except Exception:
            raw_global_haircut=1.0

    # Small samples must not receive the full mature-learning penalty. Shrink
    # the empirical haircut toward the neutral prior (1.0) and let it earn
    # authority gradually as independent completed episodes accumulate.
    global_credibility=(float(episodes)/(float(episodes)+60.0)) if episodes>0 else 0.0
    global_haircut=1.0-global_credibility*(1.0-raw_global_haircut)

    profile,key=_v90r29_profile_for_row(row)
    profile_n=int((profile or {}).get('n') or 0)
    raw_profile_haircut=1.0
    if profile and profile_n>=8:
        realized=_v90r29_num(profile.get('avg_movement_realization_ratio'))
        if realized is not None:
            raw_profile_haircut=_clip(
                0.55+0.45*max(0.0,min(1.0,float(realized))),0.60,0.95
            )
    profile_credibility=(float(profile_n)/(float(profile_n)+24.0)) if profile_n>0 else 0.0
    profile_haircut=1.0-profile_credibility*(1.0-raw_profile_haircut)

    haircut=_clip(min(global_haircut,profile_haircut),0.60,1.0)

    raw_expected=_v90r29_num(guard.get('expected_move_pct'))
    if raw_expected is None:
        plan=row.get('trade_plan') or {}
        raw_expected=_v90r29_num(plan.get('expected_move_pct'),0.0)
    raw_expected=abs(float(raw_expected or 0.0))

    raw_rr=_v90r29_num(guard.get('net_reward_risk'))
    if raw_rr is None:
        raw_rr=_v90r29_num((row.get('trade_plan') or {}).get('expected_to_stop_ratio'),0.0)
    raw_rr=float(raw_rr or 0.0)

    cost=abs(float(_v90r29_num(guard.get('modeled_round_trip_cost_pct'),0.0) or 0.0))
    calibrated_expected=raw_expected*haircut
    calibrated_rr=raw_rr*haircut
    cost_to_edge=(cost/calibrated_expected) if calibrated_expected>1e-12 else 999.0

    return {
      'active':bool(episodes>=10 or profile_n>=8),
      'episodes':episodes,
      'raw_global_edge_haircut':raw_global_haircut,
      'global_learning_credibility':global_credibility,
      'global_edge_haircut':global_haircut,
      'raw_profile_edge_haircut':raw_profile_haircut,
      'profile_learning_credibility':profile_credibility,
      'profile_edge_haircut':profile_haircut,
      'applied_edge_haircut':haircut,
      'raw_expected_move_pct':raw_expected,
      'calibrated_expected_move_pct':calibrated_expected,
      'raw_net_reward_risk':raw_rr,
      'calibrated_net_reward_risk':calibrated_rr,
      'modeled_round_trip_cost_pct':cost,
      'calibrated_cost_to_edge_ratio':cost_to_edge,
      'profile_key':list(key) if key else None,
      'profile':dict(profile) if profile else None,
    }

def _v90_candidate_profit_guard(row,policy,economics):
    base=dict(_v90r43_base_candidate_guard(row,policy,economics) or {})
    learn=_v90r43_learning_edge(row,base)
    blockers=list(base.get('blockers') or [])
    mode=str((policy or {}).get('mode') or 'CORE')
    soft_warnings=[]
    size_multiplier=1.0
    size_cap=None

    calibrated_expected=None
    calibrated_rr=None
    if learn.get('active'):
        calibrated_expected=float(learn.get('calibrated_expected_move_pct') or 0.0)
        calibrated_rr=float(learn.get('calibrated_net_reward_risk') or 0.0)
        calibrated_cost=float(learn.get('calibrated_cost_to_edge_ratio') or 999.0)

        # Friction and genuinely negative learned economics remain hard vetoes.
        max_cost_ratio=0.30 if mode in ('IMPULSE_ONLY','AGGRESSIVE') else 0.25
        if calibrated_expected<=0 or calibrated_cost>max_cost_ratio:
            blockers.append('LEARNED_COST_TO_REALIZED_EDGE_TOO_HIGH')

        learned_rr_floor={
          'IMPULSE_ONLY':1.15,
          'AGGRESSIVE':1.20,
          'CORE':1.35,
          'CHALLENGER':1.50,
        }.get(mode,1.20)
        if calibrated_rr<learned_rr_floor:
            blockers.append('LEARNED_CALIBRATED_RR_TOO_LOW')

        state=str(base.get('breakout_state') or '')
        regime=str((row or {}).get('regime') or '')
        if state=='EARLY_BREAKOUT':
            early_rr_floor={
              'IMPULSE_ONLY':1.30,
              'AGGRESSIVE':1.35,
              'CORE':1.60,
              'CHALLENGER':1.70,
            }.get(mode,1.35)
            cost=float(learn.get('modeled_round_trip_cost_pct') or 0.0)
            indep=int(base.get('independent') or 0)
            alignment=int(base.get('alignment_count') or 0)
            hscore=float(base.get('horizon_structure_score') or 0.0)
            weak_confirmation=bool(
                indep<3 or hscore<0.68
                or (mode in ('AGGRESSIVE','CORE','CHALLENGER') and alignment<2)
            )
            if (calibrated_rr<early_rr_floor
                    or calibrated_expected<max(0.006,3.0*cost)
                    or weak_confirmation):
                blockers.append('LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL')
            if regime.startswith('RANGE_'):
                blockers.append('EARLY_BREAKOUT_IN_RANGE_REGIME')

        # Mature context evidence about wrong direction/cost drag stays hard.
        profile=learn.get('profile') or {}
        pn=int(profile.get('n') or 0)
        if pn>=8:
            entry_error=float(profile.get('entry_error_rate') or 0.0)
            cost_drag=float(profile.get('cost_drag_rate') or 0.0)
            stop_error=float(profile.get('stop_error_rate') or 0.0)
            exit_error=float(profile.get('exit_capture_error_rate') or 0.0)
            overforecast=float(profile.get('overforecast_rate') or 0.0)
            bayes=float(profile.get('bayesian_win_rate') or 0.5)
            avg_pnl=float(profile.get('avg_net_pnl_rub') or 0.0)
            management_error_rate=min(1.0,stop_error+exit_error)
            management_dominated=bool(
                entry_error<0.30
                and management_error_rate>=0.30
            )
            if entry_error>=0.35:
                blockers.append('LEARNED_ENTRY_DIRECTION_ERROR_CLUSTER')
            if cost_drag>=0.45:
                blockers.append('LEARNED_COST_DRAG_CLUSTER')
            if bayes<0.45 and avg_pnl<=0:
                if mode in ('IMPULSE_ONLY','AGGRESSIVE') and management_dominated:
                    # R52: a losing historical P&L caused substantially by stop /
                    # exit-management errors is not evidence that direction is bad.
                    # Preserve a small research probe; the economics gate and
                    # calibrated RR remain authoritative.
                    soft_warnings.append(
                        'LEARNED_NEGATIVE_CONTEXT_EXPECTANCY_MANAGEMENT_DOMINATED'
                    )
                    size_cap=(0.10 if mode=='IMPULSE_ONLY' else 0.15) if size_cap is None else min(
                        size_cap,0.10 if mode=='IMPULSE_ONLY' else 0.15
                    )
                    size_multiplier=min(size_multiplier,0.60)
                else:
                    blockers.append('LEARNED_NEGATIVE_CONTEXT_EXPECTANCY')
            base['learning_attribution']={
              'entry_error_rate':entry_error,
              'cost_drag_rate':cost_drag,
              'stop_error_rate':stop_error,
              'exit_capture_error_rate':exit_error,
              'management_error_rate':management_error_rate,
              'overforecast_rate':overforecast,
              'management_dominated':management_dominated,
              'negative_expectancy_is_directional':not management_dominated,
            }

        base['raw_expected_move_pct']=learn.get('raw_expected_move_pct')
        base['raw_net_reward_risk']=learn.get('raw_net_reward_risk')
        base['expected_move_pct']=learn.get('calibrated_expected_move_pct')
        base['net_reward_risk']=learn.get('calibrated_net_reward_risk')
        base['cost_to_edge_ratio']=learn.get('calibrated_cost_to_edge_ratio')

    # R47: learning uncertainty changes research-book SIZE before it becomes a
    # binary veto. Safety/economics/freshness remain hard. Champion/Challenger
    # remain strict production candidates.
    if mode in ('IMPULSE_ONLY','AGGRESSIVE'):
        regime=str((row or {}).get('regime') or '')
        indep=int(base.get('independent') or 0)
        alignment=int(base.get('alignment_count') or 0)
        hscore=float(base.get('horizon_structure_score') or 0.0)
        non_range=not regime.startswith('RANGE_')
        structural_probe_ok=bool(non_range and indep>=2 and alignment>=1 and hscore>=0.58)
        probe_rr_floor=1.00 if mode=='IMPULSE_ONLY' else 1.05

        def soften(code, cap, mult):
            nonlocal size_cap, size_multiplier
            if code in blockers:
                blockers.remove(code)
                soft_warnings.append(code)
                size_cap=cap if size_cap is None else min(size_cap,cap)
                size_multiplier=min(size_multiplier,mult)

        if structural_probe_ok:
            soften('WEAK_BREAKOUT_NEGATIVE_HISTORY',
                   0.05 if mode=='IMPULSE_ONLY' else 0.10,0.60)
            if calibrated_rr is not None and calibrated_rr>=probe_rr_floor:
                soften('LEARNED_CALIBRATED_RR_TOO_LOW',
                       0.10 if mode=='IMPULSE_ONLY' else 0.15,0.70)

        state=str(base.get('breakout_state') or '')
        early_probe_ok=bool(
            state=='EARLY_BREAKOUT' and non_range and indep>=3
            and alignment>=2 and hscore>=0.68
            and calibrated_rr is not None and calibrated_rr>=probe_rr_floor
        )
        if early_probe_ok:
            soften('LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL',0.05,0.55)

    blockers=list(dict.fromkeys(blockers))
    soft_warnings=list(dict.fromkeys(soft_warnings))
    base['blockers']=blockers
    base['soft_warnings']=soft_warnings
    base['eligible']=not blockers
    base['status']='PASS' if base['eligible'] else 'BLOCK'
    base['size_multiplier']=size_multiplier
    base['size_cap']=size_cap
    base['admission_policy']='HARD_SAFETY_ATTRIBUTED_LEARNING_R52'
    base['closed_loop_learning_r43']=learn
    return base

def _signal_first_admission(row,policy,drawdown):
    out=dict(_v90r43_base_admission(row,policy,drawdown) or {})
    if not out.get('open'):
        return out

    profile,key=_v90r29_profile_for_row(row)
    if not profile or int(profile.get('n') or 0)<8:
        out['r43_context_learning']='INSUFFICIENT_CONTEXT_SAMPLE'
        return out

    mode=str((policy or {}).get('mode') or 'CORE')
    mult=float(profile.get('entry_size_multiplier') or 1.0)

    # Preserve R29's bounded exploration policy. Negative evidence reduces size;
    # positive evidence only modestly increases an already-admitted trade.
    if mult>1.0:
        mult=min(mult,1.15 if mode in ('AGGRESSIVE','IMPULSE_ONLY') else 1.08)
    else:
        mult=max(mult,0.55 if mode in ('AGGRESSIVE','IMPULSE_ONLY') else 0.65)

    before=float(out.get('fraction') or 0.0)
    after=_clip(
        _round_step(before*mult),
        0.0,
        float((policy or {}).get('max_fraction') or 5.0)
    )
    out['fraction']=after
    out['open']=bool(after>0)
    out['r43_context_learning']={
      'profile_key':list(key) if key else None,
      'episodes':int(profile.get('n') or 0),
      'bayesian_win_rate':profile.get('bayesian_win_rate'),
      'avg_net_pnl_rub':profile.get('avg_net_pnl_rub'),
      'entry_error_rate':profile.get('entry_error_rate'),
      'cost_drag_rate':profile.get('cost_drag_rate'),
      'exit_capture_error_rate':profile.get('exit_capture_error_rate'),
      'stop_error_rate':profile.get('stop_error_rate'),
      'management_bias':profile.get('management_bias'),
      'applied_size_multiplier':mult,
      'fraction_before':before,
      'fraction_after':after,
    }
    if isinstance(row,dict):
        row['_r43_context_learning']=out['r43_context_learning']
    return out

def report(pg_connect):
    d=dict(_v90r43_base_report(pg_connect) or {})
    d['closed_loop_learning_r43']={
      'status':'ACTIVE',
      'started_at':V90_R43_STARTED_AT,
      'final_r42_feedback_bridge':True,
      'eligible_closed_episodes':int((_v90r29_cache.get('summary') or {}).get('eligible_episodes') or 0),
      'attribution_counts':dict((_v90r29_cache.get('summary') or {}).get('attribution_counts') or {}),
      'global_edge_haircut':float(_v90r33_cache.get('edge_haircut') or 1.0),
      'global_overforecast_rate':float(_v90r33_cache.get('overforecast_rate') or 0.0),
      'global_entry_error_rate':float(_v90r33_cache.get('entry_error_rate') or 0.0),
      'global_exit_capture_error_rate':float(_v90r33_cache.get('exit_capture_error_rate') or 0.0),
      'context_profile_count':len(_v90r29_cache.get('profiles') or {}),
      'candidate_context_vetoes':[
        'entry_direction_error_cluster',
        'cost_drag_cluster',
        'negative_context_expectancy',
        'calibrated_reward_risk',
      ],
      'management_errors_penalize_direction':False,
      'research_books_keep_bounded_exploration':True,
      'profitability_guaranteed':False,
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),25)

# VERITAS V90 LEARNING DATA HYGIENE R44
# Administrative portfolio migrations/rebases are accounting events, not market
# outcomes. Keep them in the trade journal and portfolio P/L, but never allow
# them to teach direction, entry quality, expected move, stop quality or exit
# capture. This also sanitizes already-created R29 episodes.

V90_R44_STARTED_AT=os.getenv(
    'VERITAS_R44_STARTED_AT','2026-09-29T20:08:00+00:00'
)

_v90r44_base_episode_from_trade=_v90r29_episode_from_trade
_v90r44_base_step_all=step_all
_v90r44_base_report=report
_v90r44_sanitize_state={
  'at':0.0,
  'last_changed':0,
  'total_admin_excluded':0,
  'last_error':None,
}

def _v90r44_administrative_exit(reason):
    u=str(reason or '').upper().strip()
    # "REBASE" is reserved for engine / migration accounting closures in v9.0.
    # Market exits use STOP / TAKE_PROFIT / TRAIL / STRUCTURE_* / thesis reasons.
    return bool(u and 'REBASE' in u)

def _v90r29_episode_from_trade(t):
    e=dict(_v90r44_base_episode_from_trade(t) or {})
    raw_payload=_v90j_json((t or {}).get('payload'))
    ep_payload=dict(e.get('payload') or {})
    reason=str(
        ep_payload.get('exit_reason')
        or raw_payload.get('exit_reason')
        or raw_payload.get('close_reason')
        or ''
    )
    if _v90r44_administrative_exit(reason):
        original=e.get('primary_attribution')
        e['learning_eligible']=False
        e['primary_attribution']='ADMINISTRATIVE_EXIT_EXCLUDED'
        e['attributions']=['ADMINISTRATIVE_EXIT_EXCLUDED']
        e['learning_action']='EXCLUDE_FROM_LEARNING'
        ep_payload.update({
          'administrative_exit':True,
          'learning_exclusion_reason':'ADMINISTRATIVE_REBASE',
          'excluded_original_primary_attribution':original,
          'exit_reason':reason,
        })
        e['payload']=ep_payload
    return e

def _v90r44_sanitize_learning(pg_connect,force=False):
    import veritas_learning_integrity as VLI
    now=time.time()
    if (not force
            and now-float(_v90r44_sanitize_state.get('at') or 0.0)<50.0):
        return dict(_v90r44_sanitize_state)

    changed=0
    total=0
    err=None
    evidence_revalidation=None
    try:
        # Production connections use autocommit. Keep staging, row locks and
        # relabelling in one transaction; SET LOCAL then bounds the actual work.
        with pg_connect() as c, VLI.revalidation_transaction(c):
            c.execute("SET LOCAL statement_timeout = '4000ms'")
            _v90r29_ensure(c)
            evidence_revalidation=VLI.revalidate_eligible(c)
            changed=evidence_revalidation['staged']+evidence_revalidation['processed']
            cur=c.execute("""
              UPDATE v90_learning_episodes
              SET learning_eligible=FALSE,
                  primary_attribution='ADMINISTRATIVE_EXIT_EXCLUDED',
                  attributions='["ADMINISTRATIVE_EXIT_EXCLUDED"]'::jsonb,
                  learning_action='EXCLUDE_FROM_LEARNING',
                  payload=COALESCE(payload,'{}'::jsonb) || jsonb_build_object(
                    'administrative_exit',TRUE,
                    'learning_exclusion_reason','ADMINISTRATIVE_REBASE',
                    'excluded_original_primary_attribution',primary_attribution,
                    'learning_excluded_at',now()
                  )
              WHERE learning_eligible=TRUE
                AND UPPER(COALESCE(payload->>'exit_reason','')) LIKE '%REBASE%'
            """)
            administrative_changed=max(0,int(getattr(cur,'rowcount',0) or 0))
            changed+=administrative_changed
            if administrative_changed:
                VLI.invalidate('sanitizer_administrative_changed')
            cur=c.execute("""
          UPDATE v90_learning_episodes e SET learning_eligible=FALSE,
            learning_action='EXCLUDE_FROM_LEARNING',
            primary_attribution='DATA_EVIDENCE_EXCLUDED',
            attributions='["DATA_EVIDENCE_EXCLUDED"]'::jsonb,
            payload=COALESCE(e.payload,'{}'::jsonb)||jsonb_build_object(
              'learning_exclusion_reason','DATA_INTEGRITY_PROXY_OR_MISSING_PATH',
              'excluded_original_primary_attribution',e.primary_attribution,
              'learning_excluded_at',now())
          FROM paper_trades t
          WHERE e.trade_id=t.trade_id AND e.learning_eligible=TRUE AND (
            UPPER(COALESCE(t.payload->>'data_integrity_status','OK')) NOT IN ('','OK','VALID','CLEAN')
            OR (t.asset IN ('NQ','NDX') AND UPPER(CONCAT_WS(' ',
                t.payload->>'entry_primary_source',t.payload->>'entry_data_latency_class',
                t.payload->'contract_identity'->>'primary_source')) ~ '(PROXY|QQQ)')
          )
            """)
            source_changed=max(0,int(getattr(cur,'rowcount',0) or 0))
            changed+=source_changed
            if source_changed:
                VLI.invalidate('sanitizer_source_changed')
            row=c.execute("""
              SELECT COUNT(*) AS n
              FROM v90_learning_episodes
              WHERE primary_attribution='ADMINISTRATIVE_EXIT_EXCLUDED'
                 OR LOWER(COALESCE(payload->>'administrative_exit','false'))='true'
            """).fetchone()
            total=int((row or {}).get('n') or 0)
    except Exception as ex:
        changed=0  # Transaction rolled back; stale readers still require verified evidence.
        evidence_revalidation=None
        err=f'{type(ex).__name__}: {ex}'[:240]

    _v90r44_sanitize_state.update({
      'at':now,
      'last_changed':changed,
      'total_admin_excluded':total,
      'last_error':err,
      'evidence_revalidation':evidence_revalidation,
    })

    if changed or err:
        # A failed read must not leave a previously learned profile in force.
        _v90r29_cache.update(at=0.0,profiles={},summary={'status':'EVIDENCE_REVALIDATION_REQUIRED'})
        _v90r33_cache.update(at=0.0,n=0,median_realization=1.0,avg_realization=1.0,
                            avg_capture=None,overforecast_rate=0.0,entry_error_rate=0.0,
                            exit_capture_error_rate=0.0,edge_haircut=1.0)
    if changed:
        print(json.dumps({
          'event':'V90_R44_LEARNING_SANITIZED',
          'changed':changed,
          'total_admin_excluded':total,
          'policy':'ADMINISTRATIVE_AND_INCOMPLETE_EVIDENCE_EXCLUDED_FROM_LEARNING',
        },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    elif err:
        print(json.dumps({
          'event':'V90_R44_SANITIZE_ERROR',
          'error':err,
        },ensure_ascii=False,separators=(',',':')),flush=True)
    return dict(_v90r44_sanitize_state)

def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):
    summary=VPG.refresh_entry_quotes(summary)
    if VPG._entry_namespace is not None:
        observed_at=_now()  # execution clock follows quote refresh, not scan start
    try:
        _v90r44_sanitize_learning(pg_connect)
    except Exception:
        pass
    return _v90r44_base_step_all(
        summary,pg_connect,model_version,observed_at,commission_rate,emit
    )

def report(pg_connect):
    try:
        hygiene=_v90r44_sanitize_learning(pg_connect)
    except Exception as ex:
        hygiene={
          'last_changed':0,'total_admin_excluded':0,
          'last_error':f'{type(ex).__name__}: {ex}'[:240],
        }
    d=dict(_v90r44_base_report(pg_connect) or {})
    d['learning_data_hygiene_r44']={
      'status':'ACTIVE' if not hygiene.get('last_error') else 'DEGRADED',
      'started_at':V90_R44_STARTED_AT,
      'administrative_rebase_is_learning_eligible':False,
      'administrative_trades_remain_in_trade_journal':True,
      'administrative_trades_remain_in_portfolio_pnl':True,
      'last_sanitized_rows':int(hygiene.get('last_changed') or 0),
      'total_administrative_episodes_excluded':int(hygiene.get('total_admin_excluded') or 0),
      'error':hygiene.get('last_error'),
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),26)

# VERITAS V90 CLEAN PRODUCTION EVIDENCE R45
# Real-capital readiness must be proven only by trades opened under the current
# closed-loop + data-hygiene logic. Migration/rebase trades and older kernels
# remain auditable but cannot satisfy the promotion gate.

V90_R45_PRODUCTION_EVIDENCE_EPOCH=os.getenv(
    'VERITAS_R45_PRODUCTION_EVIDENCE_EPOCH','2026-09-29T20:20:00+00:00'
)
V90_R45_EXECUTION_COHORT='R45_CLEAN_CLOSED_LOOP'

_v90r45_base_entry_patch=_v90j_entry_patch
_v90r45_base_candidate_metrics=_v90_candidate_metrics
_v90r45_base_readiness=production_candidate_readiness
_v90r45_base_report=report

def _v90j_entry_patch(row,z,ts):
    d=dict(_v90r45_base_entry_patch(row,z,ts) or {})
    d.update({
      'execution_cohort':V90_R45_EXECUTION_COHORT,
      'closed_loop_learning_r43':True,
      'learning_data_hygiene_r44':True,
      'production_evidence_epoch':V90_R45_PRODUCTION_EVIDENCE_EPOCH,
    })
    return d

def _v90_candidate_metrics(pg_connect,name):
    with pg_connect() as c:
        return VLR.candidate_metrics(c,name)


def production_candidate_readiness(pg_connect):
    d=dict(_v90r45_base_readiness(pg_connect) or {})
    d['epoch']=None
    d['execution_cohort']=VLR.COHORT
    d['evidence_policy']='ONLY_CURRENT_RULES_INDEPENDENT_CLEAN_TRADES'
    d['administrative_rebase_excluded']=True
    d['pre_r45_trades_count_toward_live_gate']=False
    d['principle']=(
      'Real-money promotion requires fresh post-cost evidence from the current '
      'closed-loop execution cohort; migration/rebase and older-kernel trades '
      'remain audit history only.'
    )
    return d

def report(pg_connect):
    d=dict(_v90r45_base_report(pg_connect) or {})
    d['production_evidence_r45']={
      'status':'ACTIVE',
      'epoch_policy':'FIRST_VERIFIED_CURRENT_RULE_ENTRY',
      'execution_cohort':VLR.COHORT,
      'requires_cohort_marker':True,
      'excludes_rebase_closures':True,
      'legacy_history_kept_for_audit':True,
      'live_gate_uses_fresh_comparable_evidence_only':True,
    }
    # The nested R42 report resolves production_candidate_readiness dynamically,
    # so it already contains the R45 cohort here; avoid a duplicate DB pass.
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),27)

# VERITAS V90 TREND HOLD / MOVEMENT CAPTURE R46
# Closed-trade review showed that correct directional calls were often converted
# into near-zero trades by two execution behaviours:
# 1) soft sizing deterioration could reduce an already-confirmed same-direction
#    position even though it should only block a fresh add;
# 2) TP1 always kept roughly a 50% runner regardless of trend strength.
#
# R46 separates ENTRY economics from HOLD/EXIT management and adds an earlier,
# cost-aware MFE giveback harvest while preserving a larger runner in strong trends.
# Hard stops, hard thesis invalidation, confirmed reversal, structure exhaustion
# and portfolio hard-risk controls remain authoritative.

V90_R46_STARTED_AT=os.getenv(
    'VERITAS_R46_STARTED_AT','2026-09-29T20:25:00+00:00'
)

_v90r46_base_step_one=_step_one
_v90r46_base_close_or_reduce=_close_or_reduce
_v90r46_base_report=report

def _v842_hard_thesis_exit(row):
    """Admission vetoes never substitute for a bound held-position break."""
    return VPT.hard_thesis_exit(row)

def _v90r46_hold_context(name,z,row):
    row=row or {}
    direction=str((z or {}).get('direction') or '')
    signal_direction=str(row.get('research_decision') or 'NO_TRADE')
    hs=row.get('horizon_structure') or {}
    ti=row.get('trend_impulse') or {}
    hstate=str(hs.get('state') or '')
    try:
        strength=int(_v90ph_strength(row,direction))
    except Exception:
        strength=0
    phase=str(ti.get('phase') or '')
    same_direction=bool(direction in ('LONG','SHORT') and signal_direction==direction)
    try:
        hard=bool(_v842_hard_thesis_exit(row))
    except Exception:
        hard=False

    hold=bool(
        same_direction
        and not hard
        and hstate in ('BUILDING_TREND','CONFIRMED_TREND')
        and strength>=4
    )

    if hold and phase in ('TREND_DAY','IMPULSE_TREND') and strength>=6:
        runner=0.85
    elif hold and strength>=8:
        runner=0.80
    elif hold and strength>=6:
        runner=0.75
    elif hold:
        runner=0.65
    else:
        runner=0.50

    return {
      'active':hold,
      'same_direction':same_direction,
      'hard_thesis_exit':hard,
      'horizon_state':hstate,
      'trend_strength_score':strength,
      'trend_phase':phase,
      'tp_runner_ratio':runner,
      'portfolio':str(name or ''),
    }

def _v90r46_mark_trend_hold(c,name,candidates,summary,ts):
    marked=[]
    try:
        rows=c.execute(
            "SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)
        ).fetchall()
    except Exception:
        return marked

    for z0 in rows or []:
        z=dict(z0)
        asset=str(z.get('asset') or '')
        row=(candidates or {}).get(asset)
        if not row:
            try:
                row=_v842_management_row(summary,z) or {}
            except Exception:
                row={}
        ctx=_v90r46_hold_context(name,z,row)
        patch={
          'r46_trend_hold_active':bool(ctx.get('active')),
          'r46_same_direction':bool(ctx.get('same_direction')),
          'r46_trend_strength_score':int(ctx.get('trend_strength_score') or 0),
          'r46_trend_phase':ctx.get('trend_phase'),
          'r46_horizon_state':ctx.get('horizon_state'),
          'r46_tp_runner_ratio':float(ctx.get('tp_runner_ratio') or 0.50),
          'r46_hold_updated_at':_v90j_iso(ts),
        }
        tid=z.get('active_trade_id')
        c.execute(
            "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
            "WHERE portfolio_name=%s AND asset=%s",
            (json.dumps(patch,ensure_ascii=False,default=str),name,asset)
        )
        if tid:
            c.execute(
                "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                "WHERE trade_id=%s",
                (json.dumps(patch,ensure_ascii=False,default=str),tid)
            )
        marked.append({'asset':asset,**ctx})
    return marked

def _v90r46_giveback_harvest(c,p,name,prices,nav,ts,positions=None):
    changes=[]
    try:
        rows=positions if positions is not None else c.execute(
            "SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)
        ).fetchall()
    except Exception:
        return changes

    # This percentage is only a price-move trigger. Canonical exit authority
    # must independently verify whole-cycle net after all paid and exit costs.
    net_floor_pct=max(0.15,100.0*(2.0*float(COMMISSION)+0.0003))

    for z0 in rows or []:
        z=dict(z0)
        asset=str(z.get('asset') or '')
        if asset not in (prices or {}):
            continue
        payload=_v90j_json(z.get('payload'))
        if payload.get('r46_giveback_harvest_done'):
            continue
        if str(payload.get('data_integrity_status') or 'OK') not in ('','OK'):
            continue

        try:
            px=float(prices[asset])
            entry=float(z.get('avg_entry_price') or 0.0)
            units=abs(float(z.get('units') or 0.0))
        except Exception:
            continue
        if px<=0 or entry<=0 or units<=0:
            continue

        direction=str(z.get('direction') or '')
        current_pct=100.0*((px/entry-1.0) if direction=='LONG' else (entry/px-1.0))
        mfe=max(float(payload.get('mfe_pct') or 0.0),current_pct,0.0)
        giveback=max(0.0,mfe-max(0.0,current_pct))

        if mfe<0.30 or current_pct<net_floor_pct:
            continue
        if giveback<max(0.08,0.25*mfe):
            continue

        current_frac=units*px/max(float(nav),1.0)
        try:
            runner=float(payload.get('r46_tp_runner_ratio') or 0.60)
        except Exception:
            runner=0.60
        runner=min(0.85,max(0.60,runner))
        target=_v90ph_round5(current_frac*runner)
        target=max(0.05,min(current_frac,target))
        if target>=current_frac-0.025:
            continue

        result=canonical_close_or_reduce(
            c,p,name,z,px,target,nav,ts,'R46_MFE_GIVEBACK_HARVEST'
        )
        if not result:
            continue

        patch={
          'r46_giveback_harvest_done':True,
          'r46_giveback_harvest_at':_v90j_iso(ts),
          'r46_harvest_mfe_pct':mfe,
          'r46_harvest_current_profit_pct':current_pct,
          'r46_harvest_giveback_pct':giveback,
          'r46_harvest_net_floor_pct':net_floor_pct,
          'r46_harvest_runner_ratio':runner,
          'r46_harvest_target_fraction':target,
          # This is the first profit harvest for the position; do not immediately
          # execute the legacy TP1 again in the same/next cycle.
          'r17_tp1_done':True,
          'r17_tp1_at':_v90j_iso(ts),
          'r17_tp1_price':px,
          'r33_mfe_harvest_done':True,
          'profit_exit_policy':'R46_EARLY_GIVEBACK_HARVEST_THEN_STRUCTURAL_RUNNER',
        }
        c.execute(
            "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
            "WHERE portfolio_name=%s AND asset=%s",
            (json.dumps(patch,ensure_ascii=False,default=str),name,asset)
        )
        tid=z.get('active_trade_id')
        if tid:
            c.execute(
                "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                "WHERE trade_id=%s",
                (json.dumps(patch,ensure_ascii=False,default=str),tid)
            )
        changes.append({
          'portfolio':name,'asset':asset,'direction':direction,
          'mfe_pct':mfe,'current_profit_pct':current_pct,
          'giveback_pct':giveback,'runner_ratio':runner,
          'target_fraction':target,
        })

    if changes:
        print(json.dumps(
            {'event':'V90_R46_GIVEBACK_HARVEST','changes':changes},
            ensure_ascii=False,default=str,separators=(',',':')
        ),flush=True)
    return changes

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    z=dict(z or {})
    payload=_v90j_json(z.get('payload'))

    # Entry-quality/economics deterioration may block NEW risk, but it must not
    # cut an already-confirmed same-direction trend. Explicit exit authorities
    # still pass through untouched.
    if (str(reason or '')=='SOFT_SIZE_REDUCTION'
            and bool(payload.get('r46_trend_hold_active'))):
        tid=z.get('active_trade_id')
        patch={
          'r46_soft_reduction_blocked':True,
          'r46_soft_reduction_blocked_at':_v90j_iso(ts),
          'r46_soft_reduction_requested_fraction':float(target_fraction or 0.0),
          'r46_hold_strength_score':payload.get('r46_trend_strength_score'),
          'r46_hold_horizon_state':payload.get('r46_horizon_state'),
          'r46_hold_rule':'ENTRY_GATE_CANNOT_CUT_CONFIRMED_TREND',
        }
        if tid:
            c.execute(
                "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                "WHERE trade_id=%s",
                (json.dumps(patch,ensure_ascii=False,default=str),tid)
            )
        print(json.dumps(
            {'event':'V90_R46_SOFT_REDUCTION_BLOCKED','portfolio':name,
             'asset':z.get('asset'),'direction':z.get('direction'),
             'requested_fraction':target_fraction,
             'strength':payload.get('r46_trend_strength_score')},
            ensure_ascii=False,default=str,separators=(',',':')
        ),flush=True)
        return 0.0

    return _vp_base._v90r46_base_close_or_reduce(
        c,p,name,z,price,target_fraction,nav,ts,reason
    )

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    # Refresh MFE before deciding whether profit has started to give back.
    try:
        _v90j_update_excursions(c,name,prices,ts)
    except Exception:
        pass
    _v90r46_mark_trend_hold(c,name,candidates,summary,ts)

    try:
        p,pos=_portfolio_rows(c,name)
        nav,_,_,_=_mark_nav(p,pos,prices)
        _v90r46_giveback_harvest(c,p,name,prices,nav,ts,positions=pos)
    except Exception:
        pass

    p = pos = None
    return _v90r46_base_step_one(
        c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary
    )

def report(pg_connect):
    d=dict(_v90r46_base_report(pg_connect) or {})
    d['trend_hold_r46']={
      'status':'ACTIVE',
      'started_at':V90_R46_STARTED_AT,
      'entry_gate_can_soft_reduce_confirmed_trend':False,
      'tp1_runner_ratio':{
        'ordinary':0.50,
        'confirmed_strength_4plus':0.65,
        'strong_strength_6plus':0.75,
        'very_strong_strength_8plus':0.80,
        'trend_day_or_impulse':0.85,
      },
      'mfe_giveback_harvest':{
        'minimum_mfe_pct':0.30,
        'minimum_post_cost_profit_pct':0.15,
        'giveback_trigger':'max(0.08 percentage points, 25% of MFE)',
        'runner_range':'60%-85%',
      },
      'hard_exit_authorities_unchanged':[
        'STOP','HARD_THESIS_INVALIDATION','STRUCTURE_EXHAUSTION',
        'CONFIRMED_REVERSAL','RISK_HARD_STOP'
      ],
      'profitability_admission_repair':{
        'learned_rr_floors':{
          'Impulse':1.15,'Aggressive':1.20,'Champion':1.35,'Challenger':1.50,
        },
        'early_breakout_initial_fraction':0.05,
        'range_regime_early_breakout_allowed':False,
        'invalidated_execution_horizon_exit':True,
      },
      'principle':'fresh-entry economics controls adds; existing trend exposure is managed by structure, stops and profit protection',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),28)


# VERITAS V90 CNY TREND CAPTURE / SAFE PYRAMIDING R51
# Incident-driven repair after the 2026-09-30 CNYRUBF downtrend audit.
# Goals:
# - delayed 5m research data may trigger only a small 1h-risk transition probe;
# - higher-timeframe regime alignment blocks obvious counter-trend entries;
# - targets are bounded by current structure/risk rather than unconstrained forecasts;
# - protected winners may never be pyramided into a combined stop-loss;
# - a STOP cannot be followed by an immediate same-direction re-entry on delayed CNY data.
V90_R51_STARTED_AT=os.getenv('VERITAS_R51_EPOCH','2026-09-30T12:20:00+00:00')
_v90r51_base_admission=_signal_first_admission
_v90r51_base_open_or_add=_open_or_add
_v90r51_base_candidate_book=_candidate_book_v84
_v90r51_base_aggressive_book=_v90_aggressive_candidate_book
_v90r51_base_impulse_book=_best_impulse_by_asset
_v90r51_base_report=report

def _v90r51_num(v,default=None):
    try:
        if v is None:
            return default
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _v90r51_htf_bias(summary,asset):
    weights={'1d':1.0,'3d':1.25,'7d':1.50}
    up=down=0.0
    evidence=[]
    for r in summary or []:
        if str((r or {}).get('asset') or '')!=str(asset):
            continue
        tf=str((r or {}).get('horizon') or '')
        if tf not in weights:
            continue
        regime=str((r or {}).get('regime') or '')
        decision=str((r or {}).get('research_decision') or 'NO_TRADE')
        w=weights[tf]
        if regime.startswith('UPTREND'):
            up+=w
        elif regime.startswith('DOWNTREND'):
            down+=w
        if decision=='LONG':
            up+=0.50*w
        elif decision=='SHORT':
            down+=0.50*w
        evidence.append({'horizon':tf,'regime':regime,'decision':decision})
    direction='NO_TRADE'
    if down>=2.0 and down>=up+1.0:
        direction='SHORT'
    elif up>=2.0 and up>=down+1.0:
        direction='LONG'
    return {'direction':direction,'up_score':up,'down_score':down,'evidence':evidence}

def _v90r51_mark_countertrend(row,summary):
    if not row:
        return row
    x=dict(row)
    if str(x.get('asset') or '')!='CNYRUBF':
        return x
    d=str(x.get('research_decision') or 'NO_TRADE')
    if d not in ('LONG','SHORT'):
        return x
    bias=_v90r51_htf_bias(summary,'CNYRUBF')
    opposite='SHORT' if d=='LONG' else 'LONG'
    if bias.get('direction')==opposite:
        x['_r51_countertrend_block']='R51_CNY_HIGHER_TF_REGIME_CONFLICT'
        x['_r51_higher_tf_bias']=bias
    return x

def _candidate_book_v84(summary):
    out=dict(_v90r51_base_candidate_book(summary) or {})
    for asset,row in list(out.items()):
        out[asset]=_v90r51_mark_countertrend(row,summary)
    return out

def _best_impulse_by_asset(summary):
    out=dict(_v90r51_base_impulse_book(summary) or {})
    for asset,row in list(out.items()):
        out[asset]=_v90r51_mark_countertrend(row,summary)
    c=_v90r51_cny_transition_candidate(summary)
    if c is not None:
        old=out.get('CNYRUBF')
        if old is None or old.get('_r51_countertrend_block') or float(c.get('_rank') or 0)>float(old.get('_rank') or 0):
            out['CNYRUBF']=c
    return out

def _v90r51_cny_transition_candidate(summary):
    rows=[dict(r) for r in (summary or [])
          if str((r or {}).get('asset') or '')=='CNYRUBF'
          and str((r or {}).get('horizon') or '')=='5m']
    if not rows:
        return None
    best=None
    bias=_v90r51_htf_bias(summary,'CNYRUBF')
    for x in rows:
        d=str(x.get('research_decision') or 'NO_TRADE')
        if d not in ('LONG','SHORT') or bias.get('direction')!=d:
            continue
        if not bool(x.get('source_gate_pass')) or not bool(x.get('market_open')):
            continue
        hs=x.get('horizon_structure') or {}
        hstate=str(hs.get('state') or x.get('horizon_structure_state') or '')
        hscore=_v90r51_num(hs.get('score'),_v90r51_num(x.get('horizon_structure_score'),0.0)) or 0.0
        try:
            indep=int((((x.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count')) or
                      x.get('independent_evidence_families') or 0)
        except Exception:
            indep=0
        regime=str(x.get('regime') or '')
        regime_ok=(d=='SHORT' and regime.startswith('DOWNTREND')) or (d=='LONG' and regime.startswith('UPTREND'))
        if hstate not in ('BUILDING_TREND','CONFIRMED_TREND') or hscore<0.70 or indep<4 or not regime_ok:
            continue
        plan=dict(x.get('trade_plan') or {})
        ti=dict(plan.get('trade_integrity') or {})
        ti['hard_invalidation']=False
        ti['entry_permission']='EARLY_PROBE'
        plan['trade_integrity']=ti
        plan['eligible']=True
        plan['entry_quality']='FRESH_BREAKOUT'
        plan['reason']='R51_CNY_DELAYED_5M_TO_1H_TRANSITION'
        plan['execution_timeframe']='5m delayed trigger / 1h risk'
        y=dict(x)
        y['horizon']='1h'
        y['entry_quality']='FRESH_BREAKOUT'
        y['trade_plan']=plan
        y['_r51_cny_delayed_transition']=True
        y['_r51_original_horizon']='5m'
        y['_r51_higher_tf_bias']=bias
        supporting=['5m']+[e['horizon'] for e in bias.get('evidence') or []
                            if ((d=='SHORT' and str(e.get('regime') or '').startswith('DOWNTREND'))
                                or (d=='LONG' and str(e.get('regime') or '').startswith('UPTREND')))]
        y['_supporting_horizons']=list(dict.fromkeys(supporting))
        y['_alignment_count']=len(y['_supporting_horizons'])
        y['_direction_support']={d:hscore+0.30*len(y['_supporting_horizons']),
                                 'SHORT' if d=='LONG' else 'LONG':0.0}
        y['_support_ratio']=100.0
        y['_flip_confirmed']=False
        y['_rank']=1.50+0.20*hscore+0.03*min(indep,6)+0.05*len(y['_supporting_horizons'])
        if best is None or float(y['_rank'])>float(best.get('_rank') or 0):
            best=y
    return best

def _v90_aggressive_candidate_book(summary,core_candidates):
    out=dict(_v90r51_base_aggressive_book(summary,core_candidates) or {})
    for asset,row in list(out.items()):
        out[asset]=_v90r51_mark_countertrend(row,summary)
    c=_v90r51_cny_transition_candidate(summary)
    if c is not None:
        old=out.get('CNYRUBF')
        old_bad=bool(old and old.get('_r51_countertrend_block'))
        old_plan=(old or {}).get('trade_plan') or {}
        old_gate=old_plan.get('final_economics_gate') or {}
        old_blocked=bool(old_gate and old_gate.get('status')=='BLOCK')
        if old is None or old_bad or old_blocked or float(c.get('_rank') or 0)>float((old or {}).get('_rank') or 0):
            out['CNYRUBF']=c
    return out

def _v90r51_cny_target_repair(row):
    row=row or {}
    plan=dict(row.get('trade_plan') or {})
    if str(row.get('asset') or '')!='CNYRUBF':
        return plan
    # Apply the incident repair only to the actual delayed official CNY feed.
    # Generic/synthetic CNY unit cases and future direct feeds retain their
    # normal target semantics.
    is_delayed_official=bool(
        row.get('_r51_cny_delayed_transition')
        or str(row.get('data_latency_class') or '')=='DELAYED_RESEARCH'
        or str(row.get('verification_mode') or '')=='single_direct_official'
    )
    if not is_delayed_official:
        return plan
    d=str(row.get('research_decision') or plan.get('direction') or '')
    px=_v90r51_num(row.get('price'))
    stop=_v90r51_num(plan.get('stop_price'))
    if d not in ('LONG','SHORT') or not px or px<=0 or not stop or stop<=0:
        return plan
    if (d=='LONG' and stop>=px) or (d=='SHORT' and stop<=px):
        return plan
    stop_risk=abs(px-stop)/px
    if stop_risk<=0:
        return plan

    h=str(row.get('horizon') or '')
    caps={'5m':0.008,'1h':0.015,'4h':0.020,'1d':0.035,'3d':0.050,'7d':0.070}
    cap=float(caps.get(h,0.020))
    if row.get('_r51_cny_delayed_transition'):
        cap=min(cap,0.012)

    modeled_cost=float(VX.round_trip_cost_pct(row.get('spread_bps')))
    target_net_rr=1.25 if row.get('_r51_cny_delayed_transition') else 1.30
    required=max(0.0050,target_net_rr*stop_risk+(1.0+target_net_rr)*modeled_cost)
    required=min(required,cap)

    sign=1.0 if d=='LONG' else -1.0
    old_target=_v90r51_num(plan.get('target_price') or plan.get('tactical_target_price'))
    old_move=(sign*(old_target-px)/px) if old_target else 0.0

    structural=[]
    levels=row.get('structural_levels') or {}
    names=('resistance','resistance2','next_resistance') if d=='LONG' else ('support','support2','next_support')
    for k in names:
        v=_v90r51_num(levels.get(k))
        if v and sign*(v-px)>0:
            structural.append(sign*(v-px)/px)
    for block in (row.get('range_retest_breakout') or {},
                  row.get('impulse_pivot_break') or {},
                  row.get('tactical_reversal') or {}):
        keys=('resistance','local_resistance','target_price') if d=='LONG' else ('support','local_support','target_price')
        for k in keys:
            v=_v90r51_num(block.get(k))
            if v and sign*(v-px)>0:
                structural.append(sign*(v-px)/px)

    valid_struct=[m for m in structural if required<=m<=cap]
    if valid_struct:
        desired=min(valid_struct)
        source='STRUCTURAL_LEVEL'
    else:
        desired=max(required,min(cap,old_move if old_move>0 else required))
        source='RISK_BOUNDED_FALLBACK'
    desired=min(cap,max(0.0,desired))
    if desired<=0:
        return plan

    new_target=px*(1.0+sign*desired)
    plan['target_price']=new_target
    plan['expected_move_pct']=desired
    plan['expected_to_stop_ratio']=desired/max(stop_risk,1e-9)
    plan['r51_target_repaired']=True
    plan['r51_target_source']=source
    plan['r51_original_target_price']=old_target
    plan['r51_original_target_move_pct']=old_move
    plan['r51_target_cap_pct']=cap
    plan['r51_required_move_pct']=required
    plan['r51_stop_risk_pct']=stop_risk
    return plan

def _signal_first_admission(row,policy,drawdown):
    row=row or {}
    if row.get('_r51_countertrend_block'):
        score,source=_signal_probability(row)
        return {
          'open':False,'fraction':0.0,'reason':row.get('_r51_countertrend_block'),
          'probability':float(score) if source=='EMPIRICAL_CALIBRATION' else None,
          'model_quality_score':None if source=='EMPIRICAL_CALIBRATION' else float(score),
          'probability_source':source,
          'r51_higher_tf_bias':row.get('_r51_higher_tf_bias'),
        }
    if str(row.get('asset') or '')=='CNYRUBF':
        row['trade_plan']=_v90r51_cny_target_repair(row)
    out=dict(_v90r51_base_admission(row,policy,drawdown) or {})
    if row.get('_r51_cny_delayed_transition') and out.get('open'):
        mode=str((policy or {}).get('mode') or '')
        if mode in ('AGGRESSIVE','IMPULSE_ONLY'):
            out['fraction']=min(float(out.get('fraction') or 0.0),0.05)
            out['open']=bool(out['fraction']>0)
            out['reason']='R51_CNY_DELAYED_TRANSITION_PROBE'
            out['r51_delayed_transition']=True
            out['r51_original_horizon']='5m'
            out['r51_execution_horizon']='1h'
    return out

def _v90r51_safe_combined_scale(z,row,price,nav,requested):
    z=dict(z or {})
    if not z:
        return float(requested),{'status':'NO_EXISTING_POSITION'}
    entry=_v90r51_num(z.get('avg_entry_price'))
    px=_v90r51_num(price)
    if not entry or not px or entry<=0 or px<=0 or not nav:
        return float(requested),{'status':'INVALID_INPUT'}
    direction=str(z.get('direction') or '')
    before=abs(float(z.get('units') or 0.0)*px)/max(float(nav),1.0)
    desired=max(before,float(requested or 0.0))
    payload=_v90j_json(z.get('payload'))
    assessed=VPP.assess(None,z,price=px,nav=nav,commission=COMMISSION) if False else {}
    protected=bool(VPP.is_protected(z) or payload.get('profit_protection_active'))
    profit=(px/entry-1.0) if direction=='LONG' else (entry/px-1.0)
    q=_v90r24_aggressive_quality(row)

    extra_stage='BASE_REQUEST'
    if protected and (q.get('confirmed') or q.get('super')):
        extra=0.0
        ceiling=desired
        if profit>=0.030 and q.get('super') and q.get('independent',0)>=5:
            extra=1.00; ceiling=5.0; extra_stage='PROTECTED_DEEP_TREND'
        elif profit>=0.020:
            extra=0.75; ceiling=3.0; extra_stage='PROTECTED_2PCT'
        elif profit>=0.0125:
            extra=0.50; ceiling=2.0; extra_stage='PROTECTED_1_25PCT'
        elif profit>=0.0080:
            extra=0.35; ceiling=1.50; extra_stage='PROTECTED_0_8PCT'
        elif profit>=0.0040:
            extra=0.25; ceiling=1.00; extra_stage='PROTECTED_0_4PCT'
        if extra>0:
            desired=max(desired,min(ceiling,before+extra))

    desired=min(desired,5.0)
    # R53: use the strongest actually active stop, including R48 profit lock /
    # structural trailing. The original stop alone can be stale and would make
    # safe pyramiding unnecessarily conservative.
    raw_stops=[]
    for _s in (z.get('stop_price'),payload.get('trailing_stop')):
        _v=_v90r51_num(_s)
        if _v is not None and _v>0:
            raw_stops.append(_v)
    if raw_stops:
        stop=max(raw_stops) if direction=='LONG' else min(raw_stops)
    else:
        stop=None
    safe_target=desired
    protection_lock=2.0*float(COMMISSION)+0.0001
    stop_safe=None
    if protected and desired>before+0.001:
        if not stop or stop<=0:
            safe_target=before
            stop_safe=False
        else:
            side='BUY' if direction=='LONG' else 'SELL_SHORT'
            try:
                fill=float(VX.simulated_fill(str(z.get('asset') or ''),side,px,max(0.05,desired-before)).get('fill_price') or px)
            except Exception:
                fill=px
            candidates=[]
            step=0.05
            n=int(max(0.0,desired-before)/step+1e-9)
            for i in range(1,n+1):
                cand=min(desired,before+i*step)
                add=max(0.0,cand-before)
                avg=(before*entry+add*fill)/max(cand,1e-9)
                ok=(stop>=avg*(1.0+protection_lock)) if direction=='LONG' else (stop<=avg*(1.0-protection_lock))
                if ok:
                    candidates.append(cand)
            safe_target=max(candidates) if candidates else before
            stop_safe=bool(candidates)
    return safe_target,{
      'status':'PASS','protected':protected,'profit_pct':100.0*profit,
      'before_fraction':before,'requested_fraction':float(requested or 0.0),
      'pre_stop_target_fraction':desired,'safe_target_fraction':safe_target,
      'stop_price':stop,'original_stop_price':_v90r51_num(z.get('stop_price')),
      'trailing_stop':_v90r51_num(payload.get('trailing_stop')),
      'combined_stop_protection_safe':stop_safe,
      'protection_lock_pct':100.0*protection_lock,'stage':extra_stage,'quality':q,
    }

def _v90r51_recent_cny_stop(c,name,direction,ts,seconds=900):
    try:
        r=c.execute("""SELECT direction,closed_at,payload FROM paper_trades
                       WHERE portfolio_name=%s AND asset='CNYRUBF'
                         AND closed_at IS NOT NULL
                       ORDER BY closed_at DESC LIMIT 1""",(name,)).fetchone()
        if not r or str(r.get('direction') or '')!=str(direction):
            return None
        p=_v90j_json(r.get('payload'))
        reason=str(p.get('exit_reason') or p.get('close_reason') or '')
        if 'STOP' not in reason:
            return None
        closed=r.get('closed_at')
        now_dt=ts if isinstance(ts,datetime) else datetime.fromisoformat(str(ts).replace('Z','+00:00'))
        cl_dt=closed if isinstance(closed,datetime) else datetime.fromisoformat(str(closed).replace('Z','+00:00'))
        if now_dt.tzinfo is None: now_dt=now_dt.replace(tzinfo=timezone.utc)
        if cl_dt.tzinfo is None: cl_dt=cl_dt.replace(tzinfo=timezone.utc)
        age=max(0.0,(now_dt-cl_dt).total_seconds())
        if age<float(seconds):
            return {'age_seconds':age,'cooldown_seconds':seconds,'exit_reason':reason,'closed_at':_v90j_iso(closed)}
    except Exception:
        return None
    return None

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    existing=c.execute(
        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
        (name,asset)
    ).fetchone()
    if not existing and str(asset)=='CNYRUBF':
        cd=_v90r51_recent_cny_stop(c,name,direction,ts,900)
        if cd:
            print(json.dumps({
              'event':'V90_R51_CNY_STOP_COOLDOWN','portfolio':name,'asset':asset,
              'direction':direction,**cd
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0

    if str(name)!='Aggressive' or not existing:
        return _v90r51_base_open_or_add(
            c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason
        )

    z=dict(existing)
    # Refresh protection state before deciding whether a winner may be enlarged.
    try:
        z['payload']={**_v90j_json(z.get('payload')),
                      **VPP.assess(c,z,price=price,nav=nav,now=ts,commission=COMMISSION)}
    except Exception:
        z['payload']=_v90j_json(z.get('payload'))

    safe,meta=_v90r51_safe_combined_scale(z,row,price,nav,target_fraction)
    print(json.dumps({
      'event':'V90_R51_SAFE_SCALE_CHECK','portfolio':name,'asset':asset,
      'direction':direction,**meta
    },ensure_ascii=False,default=str,separators=(',',':')),flush=True)

    # Bypass legacy R24 auto-jump; its intended leverage path is now replaced by
    # the combined-position protection test above.
    return _vp_base._v90r24_base_open_or_add(
        c,p,name,asset,direction,price,safe,nav,ts,row,
        'R51_SAFE_SCALE' if safe>float(target_fraction or 0.0)+0.001 else reason
    )

def report(pg_connect):
    d=dict(_v90r51_base_report(pg_connect) or {})
    d['cny_trend_capture_r51']={
      'status':'ACTIVE',
      'epoch':V90_R51_STARTED_AT,
      'official_source':'MOEX ISS CNYRUBF',
      'source_latency_class':'DELAYED_RESEARCH',
      'five_minute_direct_execution':False,
      'five_minute_transition_to_1h_probe':True,
      'transition_probe_fraction':0.05,
      'higher_tf_countertrend_block':True,
      'same_direction_stop_cooldown_minutes':15,
      'target_policy':'STRUCTURAL_LEVEL_ELSE_POST_COST_RISK_BOUNDED',
      'cny_target_caps':{'5m':'0.8%','1h':'1.5%','4h':'2.0%','1d':'3.5%','3d':'5.0%','7d':'7.0%'},
      'protected_scale_rule':'ADD_ONLY_IF_COMBINED_POSITION_REMAINS_NET_PROTECTED_AT_CURRENT_STOP',
      'legacy_r24_protected_jump_authoritative':False,
      'incident_reference':'2026-09-30 CNYRUBF >1% downtrend / late scale loss',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),31)


# VERITAS V90 AGGRESSIVE DYNAMIC EXPOSURE R54
# User policy:
# - once a qualified signal passes hard source/economics/risk gates, Aggressive
#   starts at 50%-100% NAV, never at a tiny discovery allocation;
# - leverage above 1x is earned dynamically after entry from price impulse
#   relative to recent realized volatility plus structure / volume / multi-TF
#   confirmation;
# - exposure is reduced as impulse quality fades even while direction persists;
# - stops are placed behind the most recent local structural extreme and ratchet
#   behind newer confirmed extrema. Stops are never loosened.
V90_R54_STARTED_AT=os.getenv('VERITAS_R54_EPOCH','2026-09-30T12:45:00+00:00')
_v90r54_base_admission=_signal_first_admission
_v90r54_base_step_one=_step_one
_v90r54_base_open_or_add=_open_or_add
_v90r54_base_close_or_reduce=_close_or_reduce
_v90r54_base_report=report

def _v90r54_metrics(row):
    row=row or {}
    hs=row.get('horizon_structure') or {}
    st=row.get('intraday_structure') or {}
    inst=row.get('institutional_signal') or {}
    direction=str(row.get('research_decision') or 'NO_TRADE')
    try: hscore=float(hs.get('score') or row.get('horizon_structure_score') or 0.0)
    except Exception: hscore=0.0
    try: impulse=float(row.get('impulse_score') or 0.0)
    except Exception: impulse=0.0
    try: onset=float(row.get('trend_onset_score') or 0.0)
    except Exception: onset=0.0
    try: hret=float(row.get('horizon_return') or 0.0)
    except Exception: hret=0.0
    try: rv=abs(float(row.get('realized_vol') or 0.0))
    except Exception: rv=0.0
    vol_ratio=abs(hret)/max(rv,0.0005)
    signed_ok=bool(
        (direction=='LONG' and hret>=0)
        or (direction=='SHORT' and hret<=0)
        or abs(hret)<1e-9
    )
    try:
        indep=int(((inst.get('evidence_independence') or {}).get('independent_count'))
                  or row.get('independent_evidence_families') or 0)
    except Exception:
        indep=0
    supporting=list(row.get('_supporting_horizons') or [])
    try: alignment=int(row.get('_alignment_count') or len(set(supporting)))
    except Exception: alignment=0
    try: relvol=float(st.get('relative_volume') if st.get('relative_volume') is not None else row.get('relative_volume') or 0.0)
    except Exception: relvol=0.0
    try: efficiency=float(st.get('session_efficiency') or row.get('session_efficiency') or 0.0)
    except Exception: efficiency=0.0
    try: persistence=float(st.get('session_persistence') or row.get('session_persistence') or 0.0)
    except Exception: persistence=0.0
    hstate=str(hs.get('state') or row.get('horizon_structure_state') or '')
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    phase=str(row.get('trend_phase') or '')
    super_signal=tier in ('SUPER_LONG','SUPER_SHORT')
    breakout_state=str(((inst.get('breakout_quality') or {}).get('state')) or '')
    vol_component=min(1.0,vol_ratio/2.0)
    relvol_component=min(1.0,max(0.0,relvol)/1.5) if relvol>0 else 0.45
    score=(
        0.24*max(0.0,min(1.0,hscore))
        +0.20*max(0.0,min(1.0,impulse))
        +0.08*max(0.0,min(1.0,onset))
        +0.10*min(1.0,indep/6.0)
        +0.10*min(1.0,alignment/6.0)
        +0.08*max(0.0,min(1.0,efficiency))
        +0.06*max(0.0,min(1.0,persistence))
        +0.07*vol_component
        +0.04*relvol_component
        +0.03*(1.0 if super_signal else 0.0)
    )
    if not signed_ok:
        score*=0.65
    if hstate=='CONFIRMED_TREND':
        score=min(1.0,score+0.05)
    if phase in ('TREND_DAY','IMPULSE_TREND'):
        score=min(1.0,score+0.05)
    if breakout_state in ('HIGH_QUALITY_BREAKOUT','CONFIRMED_BREAKOUT'):
        score=min(1.0,score+0.03)
    return {
      'direction':direction,'strength':score,'horizon_structure_score':hscore,
      'horizon_structure_state':hstate,'impulse_score':impulse,
      'trend_onset_score':onset,'horizon_return':hret,'realized_vol':rv,
      'impulse_to_volatility':vol_ratio,'signed_impulse_confirmed':signed_ok,
      'independent':indep,'alignment':alignment,'relative_volume':relvol,
      'session_efficiency':efficiency,'session_persistence':persistence,
      'signal_tier':tier,'super':super_signal,'trend_phase':phase,
      'breakout_state':breakout_state,
    }

def _v90r54_initial_fraction(row):
    m=_v90r54_metrics(row)
    # A qualified signal is meaningful risk for Aggressive, not a 5%-15% probe.
    if m['super'] or m['strength']>=0.82:
        f=1.00
        stage='INITIAL_100_STRONG'
    elif m['strength']>=0.65:
        f=0.75
        stage='INITIAL_75_CONFIRMED'
    else:
        f=0.50
        stage='INITIAL_50_SIGNAL'
    return f,{**m,'stage':stage,'target_fraction':f}

def _v90r54_dynamic_fraction(row,current_fraction):
    m=_v90r54_metrics(row)
    base,_=_v90r54_initial_fraction(row)
    s=float(m['strength']); vr=float(m['impulse_to_volatility'])
    imp=float(m['impulse_score']); align=int(m['alignment'])
    indep=int(m['independent']); hscore=float(m['horizon_structure_score'])
    rv=float(m['relative_volume']); eff=float(m['session_efficiency'])
    pers=float(m['session_persistence']); super_signal=bool(m['super'])
    confirmed=m['horizon_structure_state']=='CONFIRMED_TREND'
    signed=bool(m['signed_impulse_confirmed'])

    raw=base
    stage=str(_v90r54_initial_fraction(row)[1]['stage'])
    # Leverage path: price impulse must be meaningful relative to its own recent
    # volatility; model confidence alone can never request leverage.
    if signed and confirmed and s>=0.70 and vr>=0.75:
        raw=max(raw,1.25); stage='SCALE_125'
    if signed and s>=0.76 and vr>=0.95 and imp>=0.55 and align>=3:
        raw=max(raw,1.50); stage='SCALE_150'
    if signed and s>=0.81 and vr>=1.15 and imp>=0.65 and align>=4 and (rv>=0.95 or eff>=0.52):
        raw=max(raw,2.00); stage='SCALE_200'
    if signed and s>=0.86 and vr>=1.40 and imp>=0.72 and align>=4 and indep>=4 and hscore>=0.78:
        raw=max(raw,3.00); stage='SCALE_300'
    if signed and s>=0.90 and vr>=1.70 and imp>=0.78 and align>=5 and indep>=5 and super_signal:
        raw=max(raw,4.00); stage='SCALE_400'
    if (signed and s>=0.93 and vr>=2.00 and imp>=0.82 and align>=5
            and indep>=5 and super_signal and hscore>=0.88
            and (rv>=1.10 or eff>=0.60) and pers>=0.55):
        raw=5.00; stage='SCALE_500'

    # Same-direction deterioration is an exposure-management signal.
    weakening=bool(
        not signed
        or s<0.58
        or m['horizon_structure_state'] in ('WEAK','NEUTRAL')
        or (imp<0.35 and vr<0.65)
    )
    if weakening:
        raw=0.50
        stage='REDUCE_TO_CORE_50'
    elif current_fraction>1.0 and (s<0.70 or vr<0.75 or imp<0.45):
        raw=min(raw,1.00)
        stage='REDUCE_LEVERAGE_TO_100'

    current=max(0.0,float(current_fraction or 0.0))
    # Scale down progressively while the thesis is intact; hard invalidation and
    # stop logic remain separate and can exit immediately.
    target=raw
    if current>raw+0.025:
        reduction_step=0.50 if current>2.0 else 0.25
        target=max(raw,current-reduction_step)
        stage=stage+'|STEPWISE_REDUCTION'
    target=_clip(_round_step(target),0.0,5.0)
    return target,{**m,'stage':stage,'raw_target_fraction':raw,
                  'current_fraction':current,'target_fraction':target}

def _v90r54_structural_stop(row):
    row=row or {}
    d=str(row.get('research_decision') or 'NO_TRADE')
    try: px=float(row.get('price') or 0.0)
    except Exception: px=0.0
    if d not in ('LONG','SHORT') or px<=0:
        return None
    st=row.get('intraday_structure') or {}
    pb=row.get('impulse_pivot_break') or {}
    rs=row.get('range_retest_breakout') or {}
    sl=row.get('structural_levels') or {}
    plan=row.get('trade_plan') or {}
    candidates=[]
    # Most recent confirmed swing is authoritative when available.
    for source,val in (
        ('RECENT_LOCAL_SWING',st.get('recent_swing_anchor')),
        ('IMPULSE_LOCAL_EXTREME',pb.get('local_support' if d=='LONG' else 'local_resistance')),
        ('RANGE_LOCAL_EXTREME',rs.get('support' if d=='LONG' else 'resistance')),
        ('STRUCTURAL_LEVEL',sl.get('support' if d=='LONG' else 'resistance')),
    ):
        try: v=float(val)
        except Exception: continue
        if v<=0: continue
        if (d=='LONG' and v<px) or (d=='SHORT' and v>px):
            candidates.append((source,v))
    if not candidates:
        return None
    # Preserve priority order above: recent swing first, then nearest local
    # structural substitute.
    source,anchor=candidates[0]
    try: rv=abs(float(row.get('realized_vol') or plan.get('realized_vol') or 0.0))
    except Exception: rv=0.0
    try: atr=abs(float(st.get('atr_5m') or 0.0))
    except Exception: atr=0.0
    atr_pct=(atr/px) if atr>0 and px>0 else 0.0
    buffer_pct=max(0.00030,min(0.00250,max(0.05*rv,0.10*atr_pct)))
    buffer=px*buffer_pct
    stop=anchor-buffer if d=='LONG' else anchor+buffer
    if (d=='LONG' and not (0<stop<px)) or (d=='SHORT' and not (stop>px)):
        return None
    target=None
    try: target=float(plan.get('target_price')) if plan.get('target_price') is not None else None
    except Exception: target=None
    risk=abs(px-stop)/px
    reward=(abs(target-px)/px) if target and ((d=='LONG' and target>px) or (d=='SHORT' and target<px)) else None
    return {
      'stop_price':stop,'anchor':anchor,'anchor_source':source,
      'buffer':buffer,'buffer_pct':buffer_pct,'stop_distance_pct':risk,
      'expected_to_stop_ratio':(reward/risk if reward is not None and risk>0 else None),
      'original_stop_price':plan.get('stop_price'),
    }

def _v90r54_apply_structural_stop(row):
    x=dict(row or {})
    plan=dict(x.get('trade_plan') or {})
    meta=_v90r54_structural_stop(x)
    if not meta:
        return x,None
    plan['stop_price']=meta['stop_price']
    plan['stop_distance_pct']=meta['stop_distance_pct']
    plan['stop_method']='R54_PREVIOUS_LOCAL_EXTREME'
    plan['recent_swing_anchor']=meta['anchor']
    plan['stop_anchor_source']=meta['anchor_source']
    plan['stop_volatility_buffer_pct']=meta['buffer_pct']
    if meta.get('expected_to_stop_ratio') is not None:
        plan['expected_to_stop_ratio']=meta['expected_to_stop_ratio']
    plan['r54_original_stop_price']=meta.get('original_stop_price')
    x['trade_plan']=plan
    return x,meta

def _signal_first_admission(row,policy,drawdown):
    out=dict(_v90r54_base_admission(row,policy,drawdown) or {})
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE' or not out.get('open'):
        return out
    target_meta=(row or {}).get('_r54_position_management') or {}
    target=target_meta.get('target_fraction')
    if target is None:
        target,target_meta=_v90r54_initial_fraction(row)
    # Override legacy discovery-size caps for Aggressive only. Hard source,
    # economics and risk gates have already passed in the wrapped admission.
    target=_clip(_round_step(max(0.50,float(target))),0.50,5.0)
    out['fraction']=target
    out['open']=True
    out['reason']='R54_AGGRESSIVE_DYNAMIC_EXPOSURE'
    out['r54_position_management']=target_meta
    out['r54_initial_range']='50%-100%'
    out['r54_dynamic_leverage_ceiling']=5.0
    out['five_minute_sizing_policy']='R54_AGGRESSIVE_50_100_THEN_DYNAMIC'
    out['five_minute_size_cap']=None
    out['sizing_authority']='R54_SIGNAL_STRENGTH_IMPULSE_VS_VOLATILITY'
    return out

def _v90r54_tighten_position_stop(c,name,z,row,price,ts):
    if not z or not row:
        return None
    d=str(z.get('direction') or '')
    if d!=str(row.get('research_decision') or ''):
        return None
    meta=_v90r54_structural_stop(row)
    if not meta:
        return None
    candidate=float(meta['stop_price'])
    px=float(price)
    payload=_v90j_json(z.get('payload'))
    active=[]
    for s in (z.get('stop_price'),payload.get('trailing_stop')):
        try:
            v=float(s)
            if v>0: active.append(v)
        except Exception:
            pass
    existing=(max(active) if d=='LONG' else min(active)) if active else None
    improves=bool(
        (d=='LONG' and candidate<px and (existing is None or candidate>existing))
        or (d=='SHORT' and candidate>px and (existing is None or candidate<existing))
    )
    if not improves:
        return None
    patch={
      'r54_structural_stop_active':True,
      'r54_structural_stop_at':_v90j_iso(ts),
      'r54_structural_stop_anchor':meta['anchor'],
      'r54_structural_stop_anchor_source':meta['anchor_source'],
      'r54_structural_stop_buffer_pct':meta['buffer_pct'],
      'r54_previous_effective_stop':existing,
      'trailing_stop':candidate,
      'trailing_rule':'R54_PREVIOUS_LOCAL_EXTREME',
    }
    c.execute(
        "UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
        "WHERE portfolio_name=%s AND asset=%s",
        (candidate,json.dumps(patch,ensure_ascii=False,default=str),name,z.get('asset'))
    )
    tid=z.get('active_trade_id')
    if tid:
        c.execute(
            "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
            (json.dumps(patch,ensure_ascii=False,default=str),tid)
        )
    return {'asset':z.get('asset'),'direction':d,'old_stop':existing,
            'new_stop':candidate,'anchor':meta['anchor'],
            'anchor_source':meta['anchor_source'],'buffer_pct':meta['buffer_pct']}

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE':
        return _v90r54_base_step_one(
            c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary
        )
    work={}
    try:
        p,pos=_portfolio_rows(c,name)
        nav,_,_,_=_mark_nav(p,pos,prices)
        posmap={str(z.get('asset')):dict(z) for z in pos}
    except Exception:
        nav=INITIAL_NAV_RUB; posmap={}
    stop_changes=[]
    for asset,row0 in (candidates or {}).items():
        row,stop_meta=_v90r54_apply_structural_stop(row0)
        z=posmap.get(str(asset))
        px=float(prices.get(asset,row.get('price') or 0.0))
        current=0.0
        if z and str(z.get('direction') or '')==str(row.get('research_decision') or '') and px>0:
            current=abs(float(z.get('units') or 0.0)*px)/max(float(nav),1.0)
        target,meta=_v90r54_dynamic_fraction(row,current) if current>0 else _v90r54_initial_fraction(row)
        row['_r54_position_management']=meta
        row['_r54_target_fraction']=target
        work[asset]=row
        if z and current>0:
            ch=_v90r54_tighten_position_stop(c,name,z,row,px,ts)
            if ch: stop_changes.append(ch)
            if current>target+0.025:
                patch={
                  'r54_dynamic_reduction_allowed':True,
                  'r54_dynamic_reduction_target':target,
                  'r54_dynamic_reduction_stage':meta.get('stage'),
                  'r54_dynamic_reduction_metrics':meta,
                  'r54_dynamic_reduction_at':_v90j_iso(ts),
                }
                c.execute(
                    "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                    "WHERE portfolio_name=%s AND asset=%s",
                    (json.dumps(patch,ensure_ascii=False,default=str),name,asset)
                )
    if stop_changes:
        print(json.dumps({'event':'V90_R54_STRUCTURAL_STOP_RATCHET',
                          'portfolio':name,'changes':stop_changes},
                         ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    p = pos = posmap = z = None
    return _v90r54_base_step_one(
        c,name,policy,work,prices,ruonia,usdrub,ts,commission_rate,summary
    )

def _v90r54_scale_profit_cap(z,price,requested):
    z=dict(z or {})
    try:
        entry=float(z.get('avg_entry_price') or 0.0); px=float(price)
    except Exception:
        return float(requested),{'profit_pct':None,'cap':1.0}
    if entry<=0 or px<=0:
        return float(requested),{'profit_pct':None,'cap':1.0}
    d=str(z.get('direction') or '')
    profit=(px/entry-1.0) if d=='LONG' else (entry/px-1.0)
    # Initial exposure is max 100%. Leverage is added only once price has moved
    # favorably enough to cover friction and demonstrate actual impulse.
    if profit<0.0015: cap=1.00
    elif profit<0.0030: cap=1.50
    elif profit<0.0050: cap=2.00
    elif profit<0.0080: cap=3.00
    elif profit<0.0120: cap=4.00
    else: cap=5.00
    return min(float(requested),cap),{'profit_pct':100.0*profit,'cap':cap}

def _v90r54_rebase_legacy_initial_risk(c,name,z,price,nav,target_fraction,ts):
    """Let a pre-R54 sub-50% position complete the intended initial allocation.

    R17 correctly freezes the original money-risk budget, but legacy 5%-15%
    entries would otherwise remain permanently tiny after R54. Rebase only the
    INITIAL allocation up to 100%, never leverage, and never beyond the global
    hard stop-risk budget.
    """
    z=dict(z or {})
    if not z:
        return None
    payload=_v90j_json(z.get('payload'))
    try:
        opening=float(payload.get('opening_fraction') or 0.0)
        px=float(price); nav=float(nav); target=min(1.0,float(target_fraction))
        units=abs(float(z.get('units') or 0.0))
        entry=float(z.get('avg_entry_price') or 0.0)
        stop=float(z.get('stop_price') or 0.0)
        d=str(z.get('direction') or '')
    except Exception:
        return None
    current=units*px/max(nav,1.0)
    # Only migrate legacy tiny entries. Positions already born under R54 keep
    # their original risk budget and are governed by ordinary pyramiding rules.
    if opening>=0.50 or current>=0.50 or target<=current+0.025:
        return None
    if d=='LONG' and not (0<stop<px):
        return None
    if d=='SHORT' and not (stop>px):
        return None

    add_notional=max(0.0,target*nav-units*px)
    add_units=add_notional/max(px,1e-9)
    existing_risk=(units*max(0.0,entry-stop) if d=='LONG'
                   else units*max(0.0,stop-entry))
    marginal_risk=(add_units*max(0.0,px-stop) if d=='LONG'
                   else add_units*max(0.0,stop-px))
    projected=max(0.0,existing_risk+marginal_risk)
    hard=float(nav)*float(MAX_STOP_RISK_NAV)
    if projected>hard+1e-6:
        return {
          'eligible':False,'reason':'R54_INITIAL_COMPLETION_EXCEEDS_HARD_STOP_RISK',
          'opening_fraction':opening,'current_fraction':current,
          'requested_fraction':target,'projected_stop_risk_rub':projected,
          'hard_stop_risk_rub':hard,
        }
    old_budget=None
    try: old_budget=float(payload.get('initial_risk_budget_rub'))
    except Exception: old_budget=None
    new_budget=max(float(old_budget or 0.0),projected)
    patch={
      'r54_legacy_initial_rebase':True,
      'r54_legacy_initial_rebase_at':_v90j_iso(ts),
      'r54_pre_rebase_opening_fraction':opening,
      'r54_pre_rebase_current_fraction':current,
      'r54_initial_completion_target':target,
      'r54_initial_completion_projected_stop_risk_rub':projected,
      'r54_initial_completion_hard_stop_risk_rub':hard,
      'initial_risk_budget_rub':new_budget,
      # From this point this trade is treated as having the R54 intended initial
      # allocation, not the historical tiny probe.
      'opening_fraction':target,
    }
    c.execute(
        "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
        "WHERE portfolio_name=%s AND asset=%s",
        (json.dumps(patch,ensure_ascii=False,default=str),name,z.get('asset'))
    )
    tid=z.get('active_trade_id')
    if tid:
        c.execute(
            "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
            (json.dumps(patch,ensure_ascii=False,default=str),tid)
        )
    return {'eligible':True,'old_budget_rub':old_budget,'new_budget_rub':new_budget,
            'opening_fraction':opening,'current_fraction':current,
            'requested_fraction':target,'projected_stop_risk_rub':projected,
            'hard_stop_risk_rub':hard}

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    if str(name)!='Aggressive':
        return _v90r54_base_open_or_add(
            c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason
        )
    existing=c.execute(
        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
        (name,asset)
    ).fetchone()
    requested=float(target_fraction or 0.0)
    gate_meta=None
    migration_meta=None
    if existing and str(existing.get('direction') or '')==str(direction):
        requested,gate_meta=_v90r54_scale_profit_cap(dict(existing),price,requested)
        # Complete legacy tiny initial allocations to the new 50%-100% policy
        # before treating further increases as pyramiding.
        if requested<=1.0+1e-9:
            migration_meta=_v90r54_rebase_legacy_initial_risk(
                c,name,dict(existing),price,nav,requested,ts
            )
            if migration_meta and migration_meta.get('eligible') is False:
                requested=abs(float(existing.get('units') or 0.0)*float(price))/max(float(nav),1.0)
    else:
        requested=min(requested,1.0)
    print(json.dumps({
      'event':'V90_R54_DYNAMIC_SIZE_CHECK','portfolio':name,'asset':asset,
      'direction':direction,'requested_fraction':float(target_fraction or 0.0),
      'profit_gated_fraction':requested,'profit_gate':gate_meta,
      'legacy_initial_rebase':migration_meta,
      'metrics':(row or {}).get('_r54_position_management'),
    },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return _v90r54_base_open_or_add(
        c,p,name,asset,direction,price,requested,nav,ts,row,
        'R54_DYNAMIC_IMPULSE_SCALE' if requested>1.0 else reason
    )

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    z=dict(z or {})
    payload=_v90j_json(z.get('payload'))
    if str(name)=='Aggressive' and str(reason or '')=='SOFT_SIZE_REDUCTION':
        allowed=bool(payload.get('r54_dynamic_reduction_allowed'))
        requested=_v90r51_num(payload.get('r54_dynamic_reduction_target'))
        if allowed and requested is not None and abs(float(target_fraction)-requested)<=0.051:
            print(json.dumps({
              'event':'V90_R54_DYNAMIC_REDUCTION','portfolio':name,
              'asset':z.get('asset'),'direction':z.get('direction'),
              'from_fraction':abs(float(z.get('units') or 0.0)*float(price))/max(float(nav),1.0),
              'to_fraction':float(target_fraction),
              'stage':payload.get('r54_dynamic_reduction_stage'),
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return _v90r46_base_close_or_reduce(
                c,p,name,z,price,target_fraction,nav,ts,'R54_DYNAMIC_IMPULSE_REDUCTION'
            )
    return _v90r54_base_close_or_reduce(
        c,p,name,z,price,target_fraction,nav,ts,reason
    )

def report(pg_connect):
    d=dict(_v90r54_base_report(pg_connect) or {})
    d['aggressive_dynamic_exposure_r54']={
      'status':'ACTIVE',
      'started_at':V90_R54_STARTED_AT,
      'initial_signal_fraction':{'ordinary':0.50,'confirmed':0.75,'strong_or_super':1.00},
      'post_entry_scale_path':[1.25,1.50,2.00,3.00,4.00,5.00],
      'scale_inputs':[
        'price_impulse_vs_realized_volatility','impulse_score','trend_onset_score',
        'multi_timeframe_alignment','independent_evidence','relative_volume',
        'session_efficiency','session_persistence','horizon_structure'
      ],
      'leverage_requires_favorable_price_progress':True,
      'same_direction_weakening_reduces_exposure':True,
      'reduction_style':'STEPWISE_25_50_PERCENT_NAV',
      'stop_policy':'PREVIOUS_LOCAL_EXTREME_PLUS_VOLATILITY_BUFFER',
      'stop_priority':[
        'recent_local_swing','impulse_local_extreme','range_local_extreme','structural_support_resistance'
      ],
      'stop_never_loosened':True,
      'max_gross':5.0,
      'principle':'50-100% on qualified signal; add or cut from observed impulse, not confidence alone',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),32)


# VERITAS V90 EXECUTION DISCIPLINE R55
# Variant B:
# - Aggressive opens 50/75/100% only after the setup is genuinely admissible;
# - INVALIDATED / hard-invalidated ideas are absolute vetoes and may not be
#   resurrected by a tighter structural stop;
# - a stop / hard invalidation cannot be followed by the same-direction stale
#   setup; a fresh post-exit market event is required;
# - 5m initial size depends on higher-TF confirmation: 50% / 75% / 100%;
# - leverage adds require a fresh favorable event plus separate remaining-edge
#   economics, not confidence alone;
# - partial reductions preserve lifetime MFE/MAE in the position/trade payload.
V90_R55_STARTED_AT=os.getenv('VERITAS_R55_EPOCH','2026-09-30T19:45:00+00:00')
_v90r55_base_admission=_signal_first_admission
_v90r55_base_open_or_add=_open_or_add
_v90r55_base_close_or_reduce=_close_or_reduce
_v90r55_base_apply_structural_stop=_v90r54_apply_structural_stop
_v90r55_base_report=report

def _v90r55_dt(v):
    if v is None:
        return None
    if isinstance(v,datetime):
        d=v
    else:
        try:
            d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        except Exception:
            return None
    if d.tzinfo is None:
        d=d.replace(tzinfo=timezone.utc)
    return d

def _v90r55_invalidated(row):
    row=row or {}
    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    return bool(
        ti.get('hard_invalidation')
        or str(row.get('decision_stage') or '').upper()=='INVALIDATED'
        or str(row.get('entry_quality') or '').upper()=='INVALIDATED'
        or str(plan.get('entry_quality') or '').upper()=='INVALIDATED'
    )

def _v90r54_apply_structural_stop(row):
    if VTM.owns_row(row): return dict(row or {}),None
    if _v90r55_invalidated(row):
        x=dict(row or {})
        x['_r55_absolute_veto']='INVALIDATED_SETUP'
        return x,None
    return _v90r55_base_apply_structural_stop(row)

def _v90r55_5m_initial_fraction(row,out):
    row=row or {}
    if str(row.get('horizon') or '')!='5m':
        return None
    supporting=set(str(x) for x in (row.get('_supporting_horizons') or []))
    # Require actual senior-TF agreement for larger first size.
    has_1h='1h' in supporting
    has_4h=bool({'4h','1d','3d','7d'} & supporting)
    m=_v90r54_metrics(row)
    if has_1h and has_4h and m.get('super') and float(m.get('strength') or 0)>=0.72:
        return 1.00,'R55_5M_100_SENIOR_CONFIRMED'
    if has_1h:
        return 0.75,'R55_5M_75_ONE_HOUR_CONFIRMED'
    return 0.50,'R55_5M_50_LOCAL_SIGNAL_ONLY'

def _signal_first_admission(row,policy,drawdown):
    row=row or {}
    if _v90r55_invalidated(row) or row.get('_r55_absolute_veto'):
        return {
          'open':False,'fraction':0.0,'reason':'R55_ABSOLUTE_INVALIDATED_VETO',
          'hard_veto':True,'r55_invalidated':True,
        }
    out=dict(_v90r55_base_admission(row,policy,drawdown) or {})
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE' or not out.get('open'):
        return out
    s=_v90r55_5m_initial_fraction(row,out)
    if s:
        fraction,reason=s
        out['fraction']=fraction
        out['reason']='R55_AGGRESSIVE_VARIANT_B'
        out['r55_initial_size_reason']=reason
        out['r55_initial_size_fraction']=fraction
    else:
        # Non-5m keeps R54's qualified 50/75/100 start.
        out['fraction']=max(0.50,min(1.0,float(out.get('fraction') or 0.50)))
        out['reason']='R55_AGGRESSIVE_VARIANT_B'
        out['r55_initial_size_reason']='R55_1H_PLUS_50_75_100'
        out['r55_initial_size_fraction']=out['fraction']
    return out

def _v90r55_recent_failed_trade(c,name,asset,direction):
    try:
        r=c.execute(
            """SELECT trade_id,closed_at,avg_exit_price,payload
               FROM paper_trades
               WHERE portfolio_name=%s AND asset=%s AND direction=%s
                 AND status='CLOSED' AND closed_at IS NOT NULL
               ORDER BY closed_at DESC LIMIT 1""",
            (name,asset,direction)
        ).fetchone()
        return dict(r) if r else None
    except Exception:
        return None

def _v90r55_reentry_gate(c,name,asset,direction,row,price,ts):
    last=_v90r55_recent_failed_trade(c,name,asset,direction)
    if not last:
        return {'eligible':True,'reason':'NO_RECENT_FAILED_TRADE'}
    p=_v90j_json(last.get('payload'))
    reason=str(p.get('exit_reason') or p.get('close_reason') or '').upper()
    if reason not in (
        'STOP','HARD_THESIS_INVALIDATION','V842_HARD_THESIS_EXIT',
        'V84_CONFIRMED_DIRECTION_FLIP','RISK_HARD_STOP'
    ):
        return {'eligible':True,'reason':'LAST_EXIT_NOT_FAILURE'}
    closed=_v90r55_dt(last.get('closed_at'))
    now=_v90r55_dt(ts) or datetime.now(timezone.utc)
    if not closed:
        return {'eligible':False,'reason':'RECENT_FAILURE_TIME_UNKNOWN'}
    age=max(0.0,(now-closed).total_seconds())
    if age>=1800.0:
        return {'eligible':True,'reason':'FAILURE_QUARANTINE_EXPIRED','age_seconds':age}

    observed=_v90r55_dt(row.get('market_observed_at') or row.get('observed_at'))
    fresh_after_exit=bool(observed and observed>closed)
    old_setup=str(p.get('canonical_setup_id') or p.get('setup_id') or '')
    try:
        new_setup=str(_portfolio_canonical_setup_id(row) or '')
    except Exception:
        new_setup=''
    bq=str((((row.get('institutional_signal') or {}).get('breakout_quality') or {}).get('state')) or '')
    hs=row.get('horizon_structure') or {}
    try: hscore=float(hs.get('score') or row.get('horizon_structure_score') or 0.0)
    except Exception: hscore=0.0
    structural=bool(
        str(hs.get('state') or row.get('horizon_structure_state') or '') in ('BUILDING_TREND','CONFIRMED_TREND')
        and hscore>=0.70
    )
    new_setup_ok=bool(new_setup and old_setup and new_setup!=old_setup)
    exit_price=_v90r51_num(last.get('avg_exit_price'))
    px=_v90r51_num(price)
    try: rv=abs(float(row.get('realized_vol') or 0.0))
    except Exception: rv=0.0
    progress_floor=min(0.006,max(0.0015,0.20*rv))
    progress=0.0
    if exit_price and px and exit_price>0:
        progress=(px/exit_price-1.0) if direction=='LONG' else (exit_price/px-1.0)
    price_event=bool(progress>=progress_floor)
    breakout_event=bq in (
        'FRESH_BREAKOUT','HIGH_QUALITY_BREAKOUT','CONFIRMED_BREAKOUT','SUPER_CONFIRMED'
    )
    eligible=bool(fresh_after_exit and structural and breakout_event and new_setup_ok and price_event)
    return {
      'eligible':eligible,
      'reason':'NEW_POST_EXIT_EVENT' if eligible else 'R55_STALE_SAME_DIRECTION_REENTRY',
      'age_seconds':age,'fresh_after_exit':fresh_after_exit,
      'old_setup_id':old_setup,'new_setup_id':new_setup,'new_setup':new_setup_ok,
      'breakout_state':bq,'structural':structural,'horizon_structure_score':hscore,
      'price_progress_from_exit':progress,'required_progress':progress_floor,
    }

def _v90r55_add_event_gate(z,row,price):
    z=dict(z or {}); row=row or {}
    if row.get('_r66_add_decision'):
        return row['_r66_add_decision']
    p=_v90j_json(z.get('payload'))
    d=str(z.get('direction') or '')
    px=_v90r51_num(price)
    if d not in ('LONG','SHORT') or not px or px<=0:
        return {'eligible':False,'reason':'R55_ADD_INVALID_INPUT'}
    m=_v90r54_metrics(row)
    plan=row.get('trade_plan') or {}
    target=_v90r51_num(plan.get('target_price') or plan.get('tactical_target_price'))
    remaining=0.0
    if target:
        remaining=((target/px)-1.0) if d=='LONG' else ((px/target)-1.0)
    remaining=max(0.0,remaining)
    cost=_v90r51_num(((plan.get('final_economics_gate') or {}).get('modeled_round_trip_cost_pct')))
    if cost is None:
        try: cost=float(VX.round_trip_cost_pct(row.get('spread_bps')))
        except Exception: cost=2.0*float(COMMISSION)
    try: rr=float((plan.get('final_economics_gate') or {}).get('net_reward_risk')
                  or row.get('_execution_rr') or plan.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0

    last_px=_v90r51_num(p.get('r55_last_scale_price'))
    last_anchor=_v90r51_num(p.get('r55_last_scale_anchor'))
    current_anchor=_v90r51_num(((row.get('intraday_structure') or {}).get('recent_swing_anchor')))
    if last_px:
        progress=(px/last_px-1.0) if d=='LONG' else (last_px/px-1.0)
    else:
        try:
            entry=float(z.get('avg_entry_price') or 0.0)
            progress=(px/entry-1.0) if d=='LONG' else (entry/px-1.0)
        except Exception:
            progress=0.0
    rv=abs(float(row.get('realized_vol') or 0.0))
    progress_floor=min(0.008,max(0.0015,0.18*rv))
    new_price_impulse=bool(progress>=progress_floor)
    new_anchor=bool(
        current_anchor is not None and (
            last_anchor is None
            or (d=='LONG' and current_anchor>last_anchor)
            or (d=='SHORT' and current_anchor<last_anchor)
        )
    )
    high_quality=bool(
        m.get('signed_impulse_confirmed')
        and float(m.get('impulse_score') or 0)>=0.55
        and float(m.get('impulse_to_volatility') or 0)>=0.75
        and int(m.get('independent') or 0)>=3
        and int(m.get('alignment') or 0)>=2
    )
    economics_ok=bool(remaining>=max(0.004,3.0*float(cost or 0.0)) and rr>=1.20)
    event_ok=bool(new_price_impulse and (new_anchor or str(m.get('breakout_state') or '') in (
        'FRESH_BREAKOUT','HIGH_QUALITY_BREAKOUT','CONFIRMED_BREAKOUT','SUPER_CONFIRMED'
    )))
    return {
      'eligible':bool(event_ok and high_quality and economics_ok),
      'reason':'R55_ADD_EVENT_CONFIRMED' if (event_ok and high_quality and economics_ok)
               else 'R55_ADD_NEEDS_NEW_IMPULSE_EVENT',
      'price_progress':progress,'required_progress':progress_floor,
      'new_anchor':new_anchor,'current_anchor':current_anchor,'last_anchor':last_anchor,
      'impulse_score':m.get('impulse_score'),
      'impulse_to_volatility':m.get('impulse_to_volatility'),
      'independent':m.get('independent'),'alignment':m.get('alignment'),
      'remaining_edge_pct':remaining,'cost_pct':cost,'net_rr':rr,
      'economics_ok':economics_ok,'event_ok':event_ok,'high_quality':high_quality,
    }

def _v90r55_mark_scale_event(c,name,z,row,price,ts):
    z=dict(z or {}); row=row or {}
    anchor=_v90r51_num(((row.get('intraday_structure') or {}).get('recent_swing_anchor')))
    patch={
      'r55_last_scale_at':_v90j_iso(ts),
      'r55_last_scale_price':float(price),
      'r55_last_scale_anchor':anchor,
      'r55_last_scale_horizon':row.get('horizon'),
      'r55_last_scale_setup_id':_portfolio_canonical_setup_id(row),
    }
    context=VTE.context_of(row);event=context.get('event') or {}
    previous=_v90j_json(z.get('payload'))
    patch.update(r66_event_id=event.get('event_id'),
                 r69_trigger_level=event.get('trigger_level'),r69_atr=event.get('atr'),
                 r66_last_confirmation_at=event.get('confirmed_at'),
                 r66_initial_fraction=previous.get('r66_initial_fraction') or previous.get('opening_fraction') or z.get('target_fraction'))
    c.execute(
        "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
        "WHERE portfolio_name=%s AND asset=%s",
        (json.dumps(patch,ensure_ascii=False,default=str),name,z.get('asset'))
    )
    tid=z.get('active_trade_id')
    if tid:
        c.execute(
            "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
            (json.dumps(patch,ensure_ascii=False,default=str),tid)
        )

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    row=row or {}
    if _v90r55_invalidated(row):
        _record_entry_outcome(row,'BLOCKED','INVALIDATED_SETUP')
        print(json.dumps({
          'event':'V90_R55_ENTRY_BLOCKED','portfolio':name,'asset':asset,
          'direction':direction,'reason':'INVALIDATED_SETUP'
        },ensure_ascii=False,separators=(',',':')),flush=True)
        return 0.0

    existing=c.execute(
        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
        (name,asset)
    ).fetchone()

    if not existing:
        rg=_v90r55_reentry_gate(c,name,asset,direction,row,price,ts)
        if not rg.get('eligible'):
            _record_entry_outcome(row,'BLOCKED',rg.get('reason') or 'REENTRY_BLOCKED')
            print(json.dumps({
              'event':'V90_R55_REENTRY_BLOCKED','portfolio':name,'asset':asset,
              'direction':direction,**rg
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0

    requested=float(target_fraction or 0.0)
    current=0.0
    if existing and str(existing.get('direction') or '')==str(direction):
        current=abs(float(existing.get('units') or 0.0)*float(price))/max(float(nav),1.0)
        if requested>current+0.025:
            # Completing a legacy pre-R54 initial allocation to <=100% is still
            # allowed under the hard stop-risk rebase. New leverage / further
            # scaling requires a new market event.
            pld=_v90j_json(existing.get('payload'))
            legacy_opening=float(pld.get('r54_pre_rebase_opening_fraction')
                                 or pld.get('opening_fraction') or current)
            opened=_v90r55_dt(existing.get('opened_at'))
            legacy_epoch=_v90r55_dt(V90_R54_STARTED_AT)
            is_legacy_completion=bool(str(name)=='Aggressive' and opened and legacy_epoch
                                     and opened<legacy_epoch and not pld.get('r17_tp1_done')
                                     and current<0.50 and requested<=1.0 and legacy_opening<0.50)
            if not is_legacy_completion:
                ag=_v90r55_add_event_gate(dict(existing),row,price)
                if not ag.get('eligible'):
                    _record_entry_outcome(row,'BLOCKED',ag.get('reason') or 'ADD_REQUIRES_NEW_EVENT')
                    print(json.dumps({
                      'event':'V90_R55_ADD_BLOCKED','portfolio':name,'asset':asset,
                      'direction':direction,'current_fraction':current,
                      'requested_fraction':requested,**ag
                    },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
                    return 0.0

    result=_v90r55_base_open_or_add(
        c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason
    )
    try:
        after=c.execute(
            "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
            (name,asset)
        ).fetchone()
        if after and str(after.get('direction') or '')==str(direction):
            after_frac=abs(float(after.get('units') or 0.0)*float(price))/max(float(nav),1.0)
            if after_frac>current+0.025:
                _v90r55_mark_scale_event(c,name,dict(after),row,price,ts)
    except Exception:
        pass
    return result

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    z=dict(z or {})
    pre=_v90j_json(z.get('payload'))
    pre_mfe=_v90r51_num(pre.get('mfe_pct'),0.0) or 0.0
    pre_mae=_v90r51_num(pre.get('mae_pct'),0.0) or 0.0
    result=_v90r55_base_close_or_reduce(
        c,p,name,z,price,target_fraction,nav,ts,reason
    )
    # A partial reduction must never erase the lifetime path of the trade.
    if result and float(target_fraction or 0.0)>0:
        try:
            z2=c.execute(
                "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
                (name,z.get('asset'))
            ).fetchone()
            if z2:
                p2=_v90j_json(z2.get('payload'))
                mfe=max(pre_mfe,float(p2.get('mfe_pct') or 0.0))
                mae=min(pre_mae,float(p2.get('mae_pct') or 0.0))
                patch={'mfe_pct':mfe,'mae_pct':mae,
                       'r55_lifetime_excursion_preserved':True}
                c.execute(
                    "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                    "WHERE portfolio_name=%s AND asset=%s",
                    (json.dumps(patch),name,z.get('asset'))
                )
                tid=z2.get('active_trade_id')
                if tid:
                    c.execute(
                        "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                        "WHERE trade_id=%s",(json.dumps(patch),tid)
                    )
        except Exception:
            pass
    return result

def report(pg_connect):
    d=dict(_v90r55_base_report(pg_connect) or {})
    d['execution_discipline_r55']={
      'status':'ACTIVE','started_at':V90_R55_STARTED_AT,
      'aggressive_variant':'B',
      'initial_size':{
        'qualified_base':'50%-100%',
        '5m_without_1h':'50%',
        '5m_plus_1h':'75%',
        '5m_plus_1h_and_4h_super':'100%',
      },
      'absolute_invalidated_veto':True,
      'same_direction_failure_quarantine_minutes':30,
      'reentry_requires_new_setup_and_post_exit_price_event':True,
      'adds_require_new_price_impulse_or_structural_extreme':True,
      'adds_require_remaining_post_cost_edge':True,
      'partial_reduction_preserves_lifetime_mfe_mae':True,
      'leverage_ceiling':5.0,
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),33)


# VERITAS V90 MULTI-TIMEFRAME TRADE FRAMING R56
# Thesis TF != Entry TF != Management TF.
# Senior horizons (1d/3d/7d) define bias only for Aggressive. A fresh 5m/1h/4h
# trigger is required to open risk. Stops/TP1 are tied to the entry/management TF.
V90_R56_STARTED_AT=os.getenv('VERITAS_R56_EPOCH','2026-10-01T03:24:00+00:00')
_v90r56_base_aggressive_book=_v90_aggressive_candidate_book
_v90r56_base_admission=_signal_first_admission
_v90r56_base_open_or_add=_open_or_add
_v90r56_base_tighten_stop=_v90r54_tighten_position_stop
_v90r56_base_step_one=_step_one
_v90r56_base_report=report

_R56_TRIGGER_HORIZONS=('1m','5m','1h','4h')
_R56_SENIOR_HORIZONS=('1d','3d','7d')

def _v90r56_direction(row):
    return str((row or {}).get('research_decision') or 'NO_TRADE')

def _v90r56_plan_pass(row):
    row=row or {}
    plan=row.get('trade_plan') or {}
    gate=plan.get('final_economics_gate') or {}
    if _v90r55_invalidated(row):
        return False
    if _v90r56_direction(row) not in ('LONG','SHORT'):
        return False
    if gate and str(gate.get('status') or '')=='BLOCK':
        return False
    return bool(plan.get('eligible',True))

def _v90r56_senior_bias(summary,asset):
    votes={'LONG':0.0,'SHORT':0.0}
    evidence=[]
    weights={'1d':1.0,'3d':1.25,'7d':1.50}
    for r in summary or []:
        if str((r or {}).get('asset') or '')!=str(asset):
            continue
        h=str((r or {}).get('horizon') or '')
        if h not in weights:
            continue
        d=_v90r56_direction(r)
        regime=str((r or {}).get('regime') or '')
        try: hs=float(((r.get('horizon_structure') or {}).get('score'))
                     or r.get('horizon_structure_score') or 0.0)
        except Exception: hs=0.0
        w=weights[h]
        if d in ('LONG','SHORT'):
            votes[d]+=w*(0.75+0.25*max(0.0,min(1.0,hs)))
        if regime.startswith('UPTREND'):
            votes['LONG']+=0.35*w
        elif regime.startswith('DOWNTREND'):
            votes['SHORT']+=0.35*w
        evidence.append({'horizon':h,'direction':d,'regime':regime,'score':hs})
    direction='NO_TRADE'
    if votes['LONG']>=1.25 and votes['LONG']>=votes['SHORT']+0.40:
        direction='LONG'
    elif votes['SHORT']>=1.25 and votes['SHORT']>=votes['LONG']+0.40:
        direction='SHORT'
    return {'direction':direction,'votes':votes,'evidence':evidence}

def _v90r56_trigger_row(summary,asset,direction):
    candidates=[]
    priority={'5m':3.0,'1h':2.5,'4h':2.0}
    for r0 in summary or []:
        if str((r0 or {}).get('asset') or '')!=str(asset):
            continue
        if str((r0 or {}).get('horizon') or '') not in _R56_TRIGGER_HORIZONS:
            continue
        if _v90r56_direction(r0)!=direction or not _v90r56_plan_pass(r0):
            continue
        r=dict(r0)
        h=str(r.get('horizon'))
        hs=r.get('horizon_structure') or {}
        try: hscore=float(hs.get('score') or r.get('horizon_structure_score') or 0.0)
        except Exception: hscore=0.0
        try:
            indep=int((((r.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'))
                      or r.get('independent_evidence_families') or 0)
        except Exception: indep=0
        q=str(r.get('entry_quality') or '')
        qbonus=0.35 if q in ('CONFIRMED_TREND','FRESH_BREAKOUT') else 0.15 if q=='NEW_SETUP_PROVISIONAL' else 0.0
        score=priority[h]+0.65*hscore+0.05*min(indep,6)+qbonus
        # Downstream portfolio routing requires _rank on every candidate.
        # Summary rows do not necessarily carry it, so R56 must always attach
        # a bounded comparable rank when promoting a trigger row.
        try:
            inherited_rank=float(r.get('_rank')) if r.get('_rank') is not None else None
        except Exception:
            inherited_rank=None
        normalized_rank=min(1.50,max(0.05,score/4.0))
        r['_r56_trigger_score']=score
        r['_rank']=max(normalized_rank,float(inherited_rank or 0.0))
        candidates.append((score,r))
    if not candidates:
        return None
    candidates.sort(key=lambda z:z[0],reverse=True)
    return candidates[0][1]

def _v90_aggressive_candidate_book(summary,core_candidates):
    out=dict(_v90r56_base_aggressive_book(summary,core_candidates) or {})
    assets=set(out)
    assets.update(str((r or {}).get('asset')) for r in (summary or []) if (r or {}).get('asset'))
    for asset in list(assets):
        senior=_v90r56_senior_bias(summary,asset)
        selected=out.get(asset)
        direction=_v90r56_direction(selected)
        thesis_h=str((selected or {}).get('horizon') or '')
        if senior.get('direction') in ('LONG','SHORT'):
            direction=senior['direction']
        if direction not in ('LONG','SHORT'):
            continue
        trigger=_v90r56_trigger_row(summary,asset,direction)
        if trigger is None:
            if selected is not None:
                x=dict(selected)
                x['_r56_missing_execution_trigger']=True
                x['_r56_thesis_horizon']=thesis_h if thesis_h in _R56_SENIOR_HORIZONS else None
                x['_r56_senior_bias']=senior
                out[asset]=x
            continue
        x=dict(trigger)
        x['_r56_entry_horizon']=str(trigger.get('horizon') or '')
        x['_r56_management_horizon']=str(trigger.get('horizon') or '')
        x['_r56_thesis_horizon']=thesis_h if thesis_h in _R56_SENIOR_HORIZONS else (
            max((e['horizon'] for e in senior.get('evidence') or [] if e.get('direction')==direction),
                key=lambda h:{'1d':1,'3d':2,'7d':3}.get(h,0),default=None)
        )
        x['_r56_senior_bias']=senior
        support=list(x.get('_supporting_horizons') or [])
        support += [e['horizon'] for e in senior.get('evidence') or []
                    if e.get('direction')==direction]
        x['_supporting_horizons']=list(dict.fromkeys(support))
        x['_alignment_count']=len(x['_supporting_horizons'])
        x['_r56_trigger_selected']=True
        out[asset]=x
    return out

def _v90r56_trigger_level(row):
    row=row or {}
    d=_v90r56_direction(row)
    blocks=[
      row.get('impulse_pivot_break') or {},
      row.get('range_retest_breakout') or {},
      row.get('tactical_reversal') or {},
      row.get('impulse_genesis') or {},
      row.get('trade_plan') or {},
    ]
    keys=('trigger_level','breakout_level','pre_impulse_swing','level')
    for b in blocks:
        if b.get('active') is False or b.get('direction',d) not in (d,None):
            continue
        for k in keys:
            v=_v90r51_num(b.get(k))
            if v and v>0:
                if d=='LONG' and v<=float(row.get('price') or v):
                    return v
                if d=='SHORT' and v>=float(row.get('price') or v):
                    return v
    return None

def _v90r56_late_entry_gate(row,price=None):
    row=row or {}
    shared=VTE.event_gate(row,price if price is not None else row.get('price'),
                         _v90r56_direction(row),datetime.now(timezone.utc))
    if shared.get('reason')!='R66_LEGACY_SIGNAL_PATH':
        return shared
    h=str(row.get('horizon') or '')
    if h not in _R56_TRIGGER_HORIZONS:
        return {'eligible':False,'reason':'R56_NO_EXECUTION_TIMEFRAME'}
    d=_v90r56_direction(row)
    reference=_v90r51_num(row.get('price'))
    px=_v90r51_num(price) if price is not None else reference
    if d not in ('LONG','SHORT') or not px or px<=0:
        return {'eligible':False,'reason':'R56_NO_DIRECTION'}
    rv=abs(_v90r51_num(row.get('realized_vol'),0.0) or 0.0)
    trigger=_v90r56_trigger_level(row)
    if trigger:
        consumed=((px/trigger)-1.0) if d=='LONG' else ((trigger/px)-1.0)
        source='TRIGGER_LEVEL'
    else:
        hr=_v90r51_num(row.get('horizon_return'))
        if hr is None or not reference or reference<=0 or hr<=-1:
            return {'eligible':False,'reason':'R65_ENTRY_ORIGIN_MISSING'}
        # Reprice against the ORIGINAL bar origin; replacing row.price alone
        # would leave a stale return unchanged and admit a late actual fill.
        hr=(1.0+hr)*px/reference-1.0
        consumed=max(0.0,hr if d=='LONG' else -hr)
        source='HORIZON_RETURN'
    # 5m/1h should not chase a move after most of its normal volatility budget
    # is consumed. 4h gets a little more room.
    ratio=0.80 if h in ('5m','1h') else 1.00
    floor={'5m':0.0040,'1h':0.0060,'4h':0.0100}.get(h,0.006)
    limit=max(floor,ratio*max(rv,0.0025))
    late=bool(consumed>limit)
    return {
      'eligible':not late,
      'reason':'R56_WAIT_RETEST_LATE_ENTRY' if late else 'R56_ENTRY_TIMING_OK',
      'consumed_move_pct':consumed,'late_entry_limit_pct':limit,
      'realized_vol':rv,'measurement_source':source,'trigger_level':trigger,
      'evaluated_price':px,'signal_price':reference,
    }

def _v90r56_stop_noise_floor(row,horizon=None):
    row=row or {}
    h=str(horizon or row.get('_r56_management_horizon') or row.get('horizon') or '1h')
    rv=abs(float(row.get('realized_vol') or 0.0))
    base={'5m':0.0020,'1h':0.0030,'4h':0.0045}.get(h,0.0045)
    mult={'5m':0.30,'1h':0.40,'4h':0.50}.get(h,0.50)
    return max(base,mult*max(rv,0.0025))

def _v90r56_entry_stop(row):
    row=row or {}
    d=_v90r56_direction(row)
    px=_v90r51_num(row.get('price'))
    if d not in ('LONG','SHORT') or not px or px<=0:
        return None
    event=VTE.context_of(row).get('event') or {}
    if row.get('asset')=='NQ' and event.get('direction')==d:
        stop=VTE.number(event.get('stop_price'))
        if stop and (px-stop)*(1 if d=='LONG' else -1)>0:
            return {'stop_price':stop,'management_horizon':'5m','noise_floor_pct':0.,
                    'structural':event,'planned_stop_price':stop,'method':'R67_IMPULSE_ORIGIN'}
    h=str(row.get('_r56_management_horizon') or row.get('horizon') or '1h')
    base=_v90r54_structural_stop(row)
    plan=row.get('trade_plan') or {}
    planned=_v90r51_num(plan.get('stop_price'))
    candidate=_v90r51_num((base or {}).get('stop_price'),planned)
    if not candidate:
        return None
    floor=_v90r56_stop_noise_floor(row,h)
    if d=='LONG':
        candidate=min(candidate,px*(1.0-floor))
        if candidate<=0 or candidate>=px: return None
    else:
        candidate=max(candidate,px*(1.0+floor))
        if candidate<=px: return None
    return {
      'stop_price':candidate,'management_horizon':h,
      'noise_floor_pct':floor,'structural':base,
      'planned_stop_price':planned,
    }

def _v90r56_structural_levels(row,direction):
    row=row or {}
    px=_v90r51_num(row.get('price')) or 0.0
    vals=[]
    def add(source,v):
        x=_v90r51_num(v)
        if not x or x<=0 or px<=0: return
        if (direction=='LONG' and x>px) or (direction=='SHORT' and x<px):
            vals.append((abs(x/px-1.0),source,x))
    sl=row.get('structural_levels') or {}
    if direction=='LONG':
        for k in ('resistance','resistance2','next_resistance'): add('STRUCTURAL_'+k.upper(),sl.get(k))
    else:
        for k in ('support','support2','next_support'): add('STRUCTURAL_'+k.upper(),sl.get(k))
    for name,b in (
        ('PIVOT',row.get('impulse_pivot_break') or {}),
        ('RANGE',row.get('range_retest_breakout') or {}),
        ('TACTICAL',row.get('tactical_reversal') or {}),
    ):
        for k in (('target_price','resistance','local_resistance') if direction=='LONG'
                  else ('target_price','support','local_support')):
            add(name+'_'+k.upper(),b.get(k))
    vals.sort(key=lambda x:x[0])
    return vals

def _v90r56_tp_plan(row,stop_meta=None):
    row=row or {}
    d=_v90r56_direction(row)
    px=_v90r51_num(row.get('price'))
    if d not in ('LONG','SHORT') or not px or px<=0:
        return None
    h=str(row.get('_r56_management_horizon') or row.get('horizon') or '1h')
    cap={'5m':0.0060,'1h':0.0120,'4h':0.0200}.get(h,0.0200)
    stop=_v90r51_num((stop_meta or {}).get('stop_price'))
    if not stop:
        stop=_v90r51_num((row.get('trade_plan') or {}).get('stop_price'))
    risk=abs(stop/px-1.0) if stop else 0.0
    min_reward=max(0.0030,1.25*risk)
    levels=_v90r56_structural_levels(row,d)
    chosen=None
    for dist,source,val in levels:
        if min_reward<=dist<=cap:
            chosen=(dist,source,val); break
    if chosen is None:
        dist=min(cap,max(min_reward,0.0060 if h!='5m' else 0.0040))
        val=px*(1.0+dist if d=='LONG' else 1.0-dist)
        source='R56_RISK_BOUNDED_TP1'
    else:
        dist,source,val=chosen
    thesis_target=_v90r51_num((row.get('trade_plan') or {}).get('target_price'))
    return {
      'tp1_price':val,'tp1_move_pct':dist,'tp1_source':source,
      'runner_target_price':thesis_target,'management_horizon':h,
      'tp1_cap_pct':cap,'risk_pct':risk,
    }

def _v90r56_prepare_entry_row(row):
    x=dict(row or {})
    if x.get('asset')=='NQ' and (VTE.context_of(x).get('event') or {}).get('event_type')=='LOCAL_RANGE_BREAKOUT':
        x=VTE.prepare_row(x)
        x['_r56_stop_plan']=_v90r56_entry_stop(x)
        plan=x.get('trade_plan') or {}
        x['_r56_tp_plan']={'tp1_price':plan.get('target_price'),
                          'runner_target_price':plan.get('r66_runner_target_price'),
                          'tp1_source':'R67_NEAREST_SENIOR_BARRIER','management_horizon':'5m'}
        return x
    plan=dict(x.get('trade_plan') or {})
    sm=_v90r56_entry_stop(x)
    if sm:
        plan['stop_price']=sm['stop_price']
        plan['stop_distance_pct']=abs(float(x.get('price'))-sm['stop_price'])/float(x.get('price'))
        plan['stop_method']='R56_ENTRY_TF_LOCAL_EXTREME'
    tp=_v90r56_tp_plan(x,sm)
    if tp:
        plan['target_price']=tp['tp1_price']
        plan['expected_move_pct']=tp['tp1_move_pct']
        if sm and plan.get('stop_distance_pct'):
            plan['expected_to_stop_ratio']=tp['tp1_move_pct']/max(float(plan['stop_distance_pct']),1e-9)
    x['trade_plan']=plan
    x['_r56_stop_plan']=sm
    x['_r56_tp_plan']=tp
    return x

def _signal_first_admission(row,policy,drawdown):
    row=row or {}
    mode=str((policy or {}).get('mode') or '')
    if mode=='AGGRESSIVE' and (
        str(row.get('horizon') or '') in _R56_SENIOR_HORIZONS
        or row.get('_r56_missing_execution_trigger')
    ):
        return {
          'open':False,'fraction':0.0,'hard_veto':True,
          'reason':'R56_SENIOR_BIAS_REQUIRES_ENTRY_TRIGGER',
          'r56_thesis_horizon':row.get('_r56_thesis_horizon') or row.get('horizon'),
        }

    # Preserve all pre-R56 hard/source/economics gates and their reason ordering.
    # Timing/entry-frame rules only act on a candidate explicitly selected by
    # the R56 5m/1h/4h trigger router.
    selected=bool(mode=='AGGRESSIVE' and row.get('_r56_trigger_selected'))
    work=row
    if selected:
        if _v90r55_invalidated(row):
            return dict(_v90r56_base_admission(row,policy,drawdown) or {})
        timing=_v90r56_late_entry_gate(row)
        if not timing.get('eligible'):
            return {
              'open':False,'fraction':0.0,'hard_veto':True,
              'reason':'R56_WAIT_RETEST_LATE_ENTRY','r56_late_entry':timing,
            }
        work=_v90r56_prepare_entry_row(row)

    out=dict(_v90r56_base_admission(work,policy,drawdown) or {})
    if mode=='AGGRESSIVE' and selected:
        out['r56_thesis_horizon']=work.get('_r56_thesis_horizon')
        out['r56_entry_horizon']=work.get('_r56_entry_horizon') or work.get('horizon')
        out['r56_management_horizon']=work.get('_r56_management_horizon') or work.get('horizon')
        out['r56_late_entry']=_v90r56_late_entry_gate(work)
        out['r56_stop_plan']=work.get('_r56_stop_plan')
        out['r56_tp_plan']=work.get('_r56_tp_plan')
    return out

def _v90r56_trailing_activation(z,row):
    z=dict(z or {}); row=row or {}
    p=_v90j_json(z.get('payload'))
    h=str(p.get('r56_management_horizon') or p.get('execution_horizon')
          or row.get('_r56_management_horizon') or row.get('horizon') or '4h')
    if h in _R56_SENIOR_HORIZONS:
        h='4h'
    try:
        entry=float(z.get('avg_entry_price') or 0.0); px=float(row.get('price') or z.get('last_price') or 0.0)
    except Exception:
        return {'active':False,'reason':'INVALID_PRICE'}
    if entry<=0 or px<=0:
        return {'active':False,'reason':'INVALID_PRICE'}
    d=str(z.get('direction') or '')
    favorable=(px/entry-1.0) if d=='LONG' else (entry/px-1.0)
    floor=_v90r56_stop_noise_floor(row,h)
    activation=max({'5m':0.0025,'1h':0.0035,'4h':0.0050}.get(h,0.0050),0.75*floor)
    return {'active':bool(favorable>=activation),'favorable_move_pct':favorable,
            'activation_pct':activation,'management_horizon':h}

def _v90r54_tighten_position_stop(c,name,z,row,price,ts):
    z=dict(z or {}); row=dict(row or {})
    if VTM.owns_position(z): return None
    row['price']=price
    if VTE.context_of(row).get('status')=='OK':
        stop=VTE.trailing_stop(z,row,price,float(VX.round_trip_cost_pct(row.get('spread_bps'))))
        if stop is not None and str(_v90j_json(z.get('payload')).get('r66_event_id','')).startswith('R69_'):
            import veritas_profit_protection as VPP
            entry=float(z.get('avg_entry_price') or 0.);d=1 if z.get('direction')=='LONG' else -1
            if d*(stop-entry)>0:
                assessed=VPP.assess(c,z,stop=stop,price=price,now=_v90r55_dt(ts),commission=COMMISSION)
                spread=max(0.,float(row.get('spread_bps') or 0.))/10000.
                cushion=abs(float(z.get('units') or 0.)*float(price))*max(.0005,2*spread+.0004)
                if float((assessed.get('net_profit_protection') or {}).get('net_at_stop_rub') or -1.)<cushion:return None
        if stop is not None:
            c.execute("UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                (stop,json.dumps({'trailing_stop':stop,'r66_trailing_at':str(ts),'trailing_method':VTE.VERSION}),name,z['asset']))
        return stop
    gate=_v90r56_trailing_activation(z,row)
    if not gate.get('active'):
        return None
    return _v90r56_base_tighten_stop(c,name,z,row,price,ts)

def _v90r56_migrate_legacy_senior_position(c,name,z,row,price,nav,ts):
    z=dict(z or {}); row=dict(row or {})
    if VTM.owns_position(z): return None
    if str(name)!='Aggressive': return None
    p=_v90j_json(z.get('payload'))
    if p.get('r56_trade_frame_migrated'): return None
    h=str(p.get('execution_horizon') or '')
    if h not in _R56_SENIOR_HORIZONS: return None
    d=str(z.get('direction') or '')
    if d not in ('LONG','SHORT'): return None
    # Legacy senior-only position is migrated to 4h management. A volatility
    # floor repairs a prematurely tightened local stop without restoring the
    # original multi-day stop.
    row['price']=price
    row['_r56_management_horizon']='4h'
    floor=_v90r56_stop_noise_floor(row,'4h')
    swing=_v90r51_num(((row.get('intraday_structure') or {}).get('recent_swing_anchor')))
    if d=='LONG':
        new_stop=float(price)*(1.0-floor)
        if swing and swing<float(price): new_stop=min(new_stop,swing-float(price)*0.0003)
    else:
        new_stop=float(price)*(1.0+floor)
        if swing and swing>float(price): new_stop=max(new_stop,swing+float(price)*0.0003)
    current_frac=abs(float(z.get('units') or 0.0)*float(price))/max(float(nav),1.0)
    stop_risk=current_frac*abs(new_stop/float(price)-1.0)
    hard=min(0.015,float(MAX_STOP_RISK_NAV))
    if stop_risk>hard:
        # Do not widen risk beyond hard budget.
        max_dist=hard/max(current_frac,0.05)
        new_stop=float(price)*(1.0-max_dist if d=='LONG' else 1.0+max_dist)
        stop_risk=current_frac*abs(new_stop/float(price)-1.0)
    sm={'stop_price':new_stop,'management_horizon':'4h','noise_floor_pct':floor}
    # The current market snapshot may be NO_TRADE/closed while an existing
    # position still needs a deterministic management target. Reuse the
    # position direction for TP1 framing and preserve any older distant target
    # only as a runner reference.
    tp_row=dict(row)
    tp_row['research_decision']=d
    tp_row['_r56_management_horizon']='4h'
    old_runner=_v90r51_num(
        p.get('r56_runner_target_price') or p.get('take_price')
        or p.get('target_price') or p.get('initial_take_price')
    )
    tp_plan=dict(tp_row.get('trade_plan') or {})
    if old_runner:
        tp_plan['target_price']=old_runner
    tp_row['trade_plan']=tp_plan
    tp=_v90r56_tp_plan(tp_row,sm)
    patch={
      'r56_trade_frame_migrated':True,'r56_trade_frame_migrated_at':_v90j_iso(ts),
      'r56_thesis_horizon':h,'r56_entry_horizon':'LEGACY_SENIOR_ONLY',
      'r56_management_horizon':'4h','r56_pre_migration_stop':z.get('stop_price'),
      'r56_pre_migration_trailing_stop':p.get('trailing_stop'),
      'r56_management_stop':new_stop,'r56_stop_risk_nav':stop_risk,
      # R56.1: protective_reason uses the strongest of stop_price/trailing_stop.
      # Leaving the old short trailing stop below the reframed 4h stop would
      # silently keep the obsolete tight stop authoritative. Reset both to the
      # same management stop and clear stale profit-protection state.
      'trailing_stop':new_stop,
      'trailing_rule':'R56_REFRAMED_4H_STOP',
      'trailing_stage':'R56_FRAME_RESET',
      'r48_profit_lock_active':False,
      'r55_net_profit_lock_active':False,
      'r56_tp1_price':(tp or {}).get('tp1_price'),
      'r56_runner_target_price':(tp or {}).get('runner_target_price'),
      'take_price':(tp or {}).get('tp1_price'),
      'target_price':(tp or {}).get('tp1_price'),
      'initial_take_price':(tp or {}).get('tp1_price'),
    }
    c.execute(
      "UPDATE paper_positions SET stop_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
      "WHERE portfolio_name=%s AND asset=%s",
      (new_stop,json.dumps(patch,ensure_ascii=False,default=str),name,z.get('asset'))
    )
    if z.get('active_trade_id'):
        c.execute(
          "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
          (json.dumps(patch,ensure_ascii=False,default=str),z.get('active_trade_id'))
        )
    print(json.dumps({
      'event':'V90_R56_LEGACY_TRADE_FRAME_MIGRATION','portfolio':name,
      'asset':z.get('asset'),'trade_id':z.get('active_trade_id'),
      'direction':d,'old_stop':z.get('stop_price'),'new_stop':new_stop,
      'tp1':(tp or {}).get('tp1_price'),'runner_target':(tp or {}).get('runner_target_price'),
      'management_horizon':'4h','stop_risk_nav':stop_risk,
    },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return patch

def _v90r56_backfill_missing_tp(c,name,z,row,price,ts):
    z=dict(z or {}); p=_v90j_json(z.get('payload'))
    if VTM.owns_position(z): return None
    if str(name)!='Aggressive' or not p.get('r56_trade_frame_migrated'):
        return None
    if _v90r51_num(p.get('r56_tp1_price')):
        return None
    d=str(z.get('direction') or '')
    if d not in ('LONG','SHORT'):
        return None
    r=dict(row or {})
    r['price']=price
    r['research_decision']=d
    r['_r56_management_horizon']=str(p.get('r56_management_horizon') or '4h')
    sm={'stop_price':_v90r51_num(z.get('stop_price')),
        'management_horizon':r['_r56_management_horizon']}
    tp=_v90r56_tp_plan(r,sm)
    if not tp or not tp.get('tp1_price'):
        return None
    patch={
      'r56_tp1_price':tp.get('tp1_price'),
      'r56_tp1_source':tp.get('tp1_source'),
      'r56_runner_target_price':tp.get('runner_target_price'),
      'take_price':tp.get('tp1_price'),
      'target_price':tp.get('tp1_price'),
      'initial_take_price':tp.get('tp1_price'),
      'r56_tp1_backfilled_at':_v90j_iso(ts),
    }
    c.execute(
      "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
      "WHERE portfolio_name=%s AND asset=%s",
      (json.dumps(patch,ensure_ascii=False,default=str),name,z.get('asset'))
    )
    if z.get('active_trade_id'):
        c.execute(
          "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
          (json.dumps(patch,ensure_ascii=False,default=str),z.get('active_trade_id'))
        )
    print(json.dumps({
      'event':'V90_R56_TP1_BACKFILL','portfolio':name,'asset':z.get('asset'),
      'trade_id':z.get('active_trade_id'),'direction':d,
      'tp1':tp.get('tp1_price'),'source':tp.get('tp1_source'),
      'runner_target':tp.get('runner_target_price'),
      'management_horizon':r['_r56_management_horizon'],
    },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    return patch

def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    if str((policy or {}).get('mode') or '')=='AGGRESSIVE':
        try:
            p,pos=_portfolio_rows(c,name)
            nav,_,_,_=_mark_nav(p,pos,prices)
            for z0 in pos:
                z=dict(z0)
                asset=str(z.get('asset') or '')
                px=float(prices.get(asset,z.get('last_price') or 0.0))
                row=(candidates or {}).get(asset)
                if row is None:
                    same=[dict(r) for r in (summary or []) if str((r or {}).get('asset') or '')==asset]
                    # Use the freshest available lower-TF context for volatility/swing
                    row=next((r for r in same if str(r.get('horizon'))=='4h'),None)                         or next((r for r in same if str(r.get('horizon'))=='1h'),None)                         or next((r for r in same if str(r.get('horizon'))=='5m'),None)
                if row is not None:
                    _v90r56_migrate_legacy_senior_position(c,name,z,row,px,nav,ts)
                    if VTM.owns_position(z): continue
                    # Re-read after migration because the first R56 live cycle may
                    # have migrated the stop before a TP could be framed.
                    zfresh=c.execute(
                        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
                        (name,asset)
                    ).fetchone()
                    if zfresh:
                        _v90r56_backfill_missing_tp(c,name,dict(zfresh),row,px,ts)
        except Exception as ex:
            print(json.dumps({'event':'V90_R56_MIGRATION_ERROR','error':f'{type(ex).__name__}: {ex}'},
                             ensure_ascii=False,separators=(',',':')),flush=True)
    p = pos = z0 = z = zfresh = None
    return _v90r56_base_step_one(
        c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary
    )

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    row=dict(row or {})
    row=VTE.prepare_row(row,price)
    if str(name)=='Aggressive':
        row=_v90r56_prepare_entry_row(row)
    before=c.execute(
        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",(name,asset)
    ).fetchone()
    result=_v90r56_base_open_or_add(
        c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason
    )
    try:
        after=c.execute(
            "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",(name,asset)
        ).fetchone()
        if after and (not before):
            tp=row.get('_r56_tp_plan') or {}
            sm=row.get('_r56_stop_plan') or {}
            patch={
              'r56_thesis_horizon':row.get('_r56_thesis_horizon'),
              'r56_entry_horizon':row.get('_r56_entry_horizon') or row.get('horizon'),
              'r56_management_horizon':row.get('_r56_management_horizon') or row.get('horizon'),
              'r56_entry_stop_price':sm.get('stop_price'),
              'r56_tp1_price':tp.get('tp1_price'),
              'r56_tp1_source':tp.get('tp1_source'),
              'r56_runner_target_price':tp.get('runner_target_price'),
              'take_price':tp.get('tp1_price'),
              'target_price':tp.get('tp1_price'),
              'initial_take_price':tp.get('tp1_price'),
            }
            # All portfolios retain a real TP1. Do not overwrite valid targets
            # with null merely because the legacy Aggressive helper did not run.
            nearest=(row.get('trade_plan') or {}).get('target_price')
            for key in ('take_price','target_price','initial_take_price','r56_tp1_price'):
                if patch.get(key) is None:patch[key]=nearest
            patch['r66_entry_geometry']=(row.get('trade_plan') or {}).get('r66_geometry')
            patch['r66_initial_fraction']=float(target_fraction)
            c.execute(
              "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
              "WHERE portfolio_name=%s AND asset=%s",
              (json.dumps(patch,ensure_ascii=False,default=str),name,asset)
            )
            tid=after.get('active_trade_id')
            if tid:
                c.execute(
                  "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                  (json.dumps(patch,ensure_ascii=False,default=str),tid)
                )
    except Exception:
        pass
    return result

def report(pg_connect):
    d=dict(_v90r56_base_report(pg_connect) or {})
    d['multi_timeframe_trade_framing_r56']={
      'status':'ACTIVE','started_at':V90_R56_STARTED_AT,
      'principle':'senior TF=bias; 5m/1h/4h=entry; entry TF=stop and TP1',
      'senior_horizons':['1d','3d','7d'],
      'entry_horizons':['5m','1h','4h'],
      'senior_only_aggressive_entry':False,
      'late_entry_policy':'block and wait for retest after 0.8-1.0x realized volatility budget',
      'trailing_activation':'only after favorable move reaches entry-TF activation threshold',
      'tp_policy':'nearest entry-TF structural level or bounded R-multiple; senior target is runner only',
      'legacy_senior_positions':'migrate to 4h management within hard stop-risk budget',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),34)


# VERITAS V90 TACTICAL TRIGGER PRIORITY R57
# Repair for 2026-10-01 NQ: a valid 1h SHORT (PASS, R/R ~1.84) was blocked
# because stale senior LONG bias was treated as an execution veto. From R57,
# senior TFs influence INITIAL SIZE; they do not cancel a fresh valid 5m/1h/4h trigger.
V90_R57_STARTED_AT=os.getenv('VERITAS_R57_EPOCH','2026-10-01T07:40:00+00:00')
_v90r57_base_aggressive_book=_v90_aggressive_candidate_book
_v90r57_base_admission=_signal_first_admission
_v90r57_base_report=report

def _v90r57_valid_trigger(row):
    row=row or {}
    if str(row.get('horizon') or '') not in _R56_TRIGGER_HORIZONS:
        return False
    if _v90r55_invalidated(row):
        return False
    if _v90r56_direction(row) not in ('LONG','SHORT'):
        return False
    plan=row.get('trade_plan') or {}
    gate=plan.get('final_economics_gate') or {}
    if str(gate.get('status') or row.get('final_gate_status') or '')=='BLOCK':
        return False
    if gate and str(gate.get('status') or '') not in ('PASS',''):
        return False
    return bool(plan.get('eligible',True))

def _v90r57_trigger_score(row):
    row=row or {}
    h=str(row.get('horizon') or '')
    base={'1h':3.40,'5m':3.20,'4h':2.60}.get(h,0.0)
    hs=row.get('horizon_structure') or {}
    try: hscore=float(hs.get('score') or row.get('horizon_structure_score') or 0.0)
    except Exception: hscore=0.0
    try:
        indep=int((((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'))
                  or row.get('independent_evidence_families') or 0)
    except Exception: indep=0
    try:
        rr=float(((row.get('trade_plan') or {}).get('final_economics_gate') or {}).get('net_reward_risk')
                 or (row.get('trade_plan') or {}).get('expected_to_stop_ratio')
                 or row.get('expected_to_stop_ratio') or 0.0)
    except Exception: rr=0.0
    try:
        move=abs(float((row.get('trade_plan') or {}).get('expected_move_pct')
                       or row.get('expected_move_pct') or 0.0))
    except Exception: move=0.0
    q=str(row.get('entry_quality') or '')
    qbonus=0.45 if q in ('CONFIRMED_TREND','FRESH_BREAKOUT') else 0.20 if q=='NEW_SETUP_PROVISIONAL' else 0.0
    return base+0.65*hscore+0.06*min(indep,6)+0.10*min(rr,3.0)+0.05*min(move/0.01,2.0)+qbonus

def _v90r57_best_trigger(summary,asset):
    rows=[]
    for r0 in summary or []:
        if str((r0 or {}).get('asset') or '')!=str(asset):
            continue
        if not _v90r57_valid_trigger(r0):
            continue
        rows.append((_v90r57_trigger_score(r0),dict(r0)))
    if not rows:
        return None
    rows.sort(key=lambda z:z[0],reverse=True)
    x=rows[0][1]
    x['_r57_trigger_score']=rows[0][0]
    return x

def _v90r57_direction_confirmation(summary,asset,direction):
    out={}
    for h in _R56_TRIGGER_HORIZONS:
        best=None
        for r0 in summary or []:
            if str((r0 or {}).get('asset') or '')!=str(asset):
                continue
            if str((r0 or {}).get('horizon') or '')!=h:
                continue
            if _v90r56_direction(r0)!=direction:
                continue
            r=dict(r0)
            hs=r.get('horizon_structure') or {}
            try: score=float(hs.get('score') or r.get('horizon_structure_score') or 0.0)
            except Exception: score=0.0
            try:
                indep=int((((r.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'))
                          or r.get('independent_evidence_families') or 0)
            except Exception: indep=0
            best={
              'score':score,'state':str(hs.get('state') or r.get('horizon_structure_state') or ''),
              'independent':indep,'plan_pass':_v90r57_valid_trigger(r),
              'entry_quality':str(r.get('entry_quality') or ''),
            }
            break
        out[h]=best
    return out

def _v90_aggressive_candidate_book(summary,core_candidates):
    out=dict(_v90r57_base_aggressive_book(summary,core_candidates) or {})
    assets=set(out)
    assets.update(str((r or {}).get('asset')) for r in (summary or []) if (r or {}).get('asset'))
    for asset in list(assets):
        trigger=_v90r57_best_trigger(summary,asset)
        if trigger is None:
            continue

        direction=_v90r56_direction(trigger)
        senior=_v90r56_senior_bias(summary,asset)
        senior_dir=str(senior.get('direction') or 'NO_TRADE')
        conflict=bool(senior_dir in ('LONG','SHORT') and senior_dir!=direction)
        aligned=bool(senior_dir==direction)
        confirm=_v90r57_direction_confirmation(summary,asset,direction)

        x=dict(trigger)
        x['_r56_trigger_selected']=True
        x['_r56_entry_horizon']=str(trigger.get('horizon') or '')
        x['_r56_management_horizon']=str(trigger.get('horizon') or '')
        x['_r57_senior_bias']=senior
        x['_r57_senior_conflict']=conflict
        x['_r57_senior_aligned']=aligned
        x['_r57_direction_confirmation']=confirm
        x['_r57_original_candidate_horizon']=str((out.get(asset) or {}).get('horizon') or '')
        _old_h=x['_r57_original_candidate_horizon']
        x['_r56_thesis_horizon']=_old_h if _old_h in _R56_SENIOR_HORIZONS else (
            max((e.get('horizon') for e in (senior.get('evidence') or [])
                 if str(e.get('direction') or '')==direction),
                key=lambda h:{'1d':1,'3d':2,'7d':3}.get(h,0),default=None)
        )

        # Senior view changes initial risk, never the existence of a valid tactical trade.
        if conflict:
            cap=0.50
            size_reason='COUNTER_SENIOR_TACTICAL_50'
        elif aligned:
            cap=1.00
            size_reason='SENIOR_ALIGNED_UP_TO_100'
        else:
            cap=0.75
            size_reason='SENIOR_NEUTRAL_UP_TO_75'
        x['_r57_initial_size_cap']=cap
        x['_r57_size_reason']=size_reason

        support=list(x.get('_supporting_horizons') or [])
        for h in ('5m','1h','4h'):
            if confirm.get(h) is not None:
                support.append(h)
        x['_supporting_horizons']=list(dict.fromkeys(support))
        x['_alignment_count']=len(x['_supporting_horizons'])
        out[asset]=x
    return out

def _signal_first_admission(row,policy,drawdown):
    out=dict(_v90r57_base_admission(row,policy,drawdown) or {})
    if str((policy or {}).get('mode') or '')!='AGGRESSIVE' or not out.get('open'):
        return out
    if not (row or {}).get('_r57_trigger_score'):
        return out

    cap=float((row or {}).get('_r57_initial_size_cap') or 1.0)
    # Variant B floor remains 50% for an admitted tactical signal.
    desired=max(0.50,float(out.get('fraction') or 0.50))
    desired=min(desired,cap)

    # A 1h PASS against stale senior bias opens 50%, not 0%.
    # If 5m simultaneously confirms a strong trend, preserve the same 50% cap
    # but flag the trade for faster event-driven scaling.
    confirm=(row or {}).get('_r57_direction_confirmation') or {}
    five=confirm.get('5m') or {}
    fast_confirm=bool(
        five
        and five.get('state') in ('BUILDING_TREND','CONFIRMED_TREND')
        and float(five.get('score') or 0.0)>=0.65
        and int(five.get('independent') or 0)>=4
    )
    out['fraction']=_round_step(desired)
    out['open']=bool(out['fraction']>0)
    out['reason']='R57_TACTICAL_TRIGGER_PRIORITY'
    out['r57_senior_conflict']=bool((row or {}).get('_r57_senior_conflict'))
    out['r57_senior_aligned']=bool((row or {}).get('_r57_senior_aligned'))
    out['r57_initial_size_cap']=cap
    out['r57_size_reason']=(row or {}).get('_r57_size_reason')
    out['r57_fast_confirmation']=fast_confirm
    out['r57_trigger_horizon']=(row or {}).get('horizon')
    return out

def report(pg_connect):
    d=dict(_v90r57_base_report(pg_connect) or {})
    d['tactical_trigger_priority_r57']={
      'status':'ACTIVE','started_at':V90_R57_STARTED_AT,
      'principle':'fresh valid 5m/1h/4h trigger may trade; senior TF changes size, not existence',
      'counter_senior_initial_cap':0.50,
      'senior_neutral_initial_cap':0.75,
      'senior_aligned_initial_cap':1.00,
      'one_hour_valid_trigger_authoritative':True,
      'stale_or_blocked_5m_cannot_execute':True,
      'leverage_still_requires_R55_new_impulse_event':True,
      'incident_reference':'NQ 2026-10-01 07:24 UTC valid 1h SHORT blocked by senior LONG bias',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),35)




# VERITAS V90 LOSS ROOT-CAUSE GATE R59
# Consolidated repair for repeated historical loss classes:
# cost drag / overforecasted edge, counter-structure entries, stops too close
# to trading friction, near-flat signal-flip churn, and same-observation reversal.
V90_R59_STARTED_AT=os.getenv('VERITAS_R59_EPOCH','2026-10-01T12:00:00+00:00')
_v90r59_base_admission=_signal_first_admission
_v90r59_base_open_or_add=_open_or_add
_v90r59_base_close_or_reduce=_close_or_reduce
_v90r59_base_stop_noise_floor=_v90r56_stop_noise_floor
_v90r59_base_report=report

def _v90r59_float(v,default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else default
    except Exception:
        return default

def _v90r59_entry_metrics(row):
    row=row or {}
    plan=row.get('trade_plan') or {}
    gate=plan.get('final_economics_gate') or {}
    rr=_v90r59_float(
        gate.get('net_reward_risk') or row.get('_execution_rr')
        or plan.get('expected_to_stop_ratio') or row.get('expected_to_stop_ratio'),0.0)
    move=abs(_v90r59_float(
        gate.get('expected_move_pct') or plan.get('expected_move_pct')
        or row.get('expected_move_pct'),0.0))
    cost=max(
        _v90r59_float(gate.get('modeled_round_trip_cost_pct'),0.0),
        _v90r59_float(VX.round_trip_cost_pct(row.get('spread_bps')),0.0))
    hs=row.get('horizon_structure') or {}
    inst=row.get('institutional_signal') or {}
    bq=inst.get('breakout_quality') or {}
    try:
        indep=int((inst.get('evidence_independence') or {}).get('independent_count')
                  or row.get('independent_evidence_families') or 0)
    except Exception:
        indep=0
    return {
      'rr':rr,'move':move,'cost':cost,
      'horizon':str(row.get('horizon') or ''),
      'direction':_v90r56_direction(row),
      'hstate':str(hs.get('state') or row.get('horizon_structure_state') or ''),
      'hdir':str(hs.get('direction') or row.get('horizon_structure_direction') or 'NO_TRADE'),
      'hscore':_v90r59_float(hs.get('score') or row.get('horizon_structure_score'),0.0),
      'independent':indep,
      'breakout_state':str(bq.get('state') or ''),
      'entry_quality':str(row.get('entry_quality') or plan.get('entry_quality') or ''),
      'tactical_reversal':row.get('tactical_reversal') or {},
    }

def _v90r59_thresholds(policy,horizon,cost):
    mode=str((policy or {}).get('mode') or 'CORE')
    rr_floor={'IMPULSE_ONLY':1.30,'AGGRESSIVE':1.35,'CORE':1.45,'CHALLENGER':1.55}.get(mode,1.45)
    if horizon=='5m':
        rr_floor+=0.10
    move_floor=VX.minimum_expected_move_pct(cost)
    return rr_floor,move_floor

def _v90r59_strong_reversal(row,metrics=None):
    m=metrics or _v90r59_entry_metrics(row)
    tr=m.get('tactical_reversal') or {}
    tr_dir=str(tr.get('direction') or '')
    explicit=bool(tr.get('active') and tr_dir in ('',m.get('direction')))
    breakout=m.get('breakout_state') in (
      'HIGH_QUALITY_BREAKOUT','CONFIRMED_BREAKOUT','SUPER_CONFIRMED')
    return bool(
      (explicit or breakout)
      and m.get('entry_quality') in ('FRESH_BREAKOUT','CONFIRMED_TREND')
      and float(m.get('hscore') or 0.0)>=0.72
      and int(m.get('independent') or 0)>=4
      and float(m.get('rr') or 0.0)>=1.50
      and float(m.get('move') or 0.0)>=max(0.0060,4.0*float(m.get('cost') or 0.0))
    )

def _v90r59_quality_gate(row,policy):
    m=_v90r59_entry_metrics(row)
    rr_floor,move_floor=_v90r59_thresholds(policy,m['horizon'],m['cost'])
    blockers=[]
    if m['rr']<rr_floor:
        blockers.append('R59_POST_COST_RR_MARGIN_TOO_LOW')
    if m['move']<move_floor:
        blockers.append('R59_POST_COST_MOVE_MARGIN_TOO_LOW')
    if m['entry_quality']=='NEW_SETUP_PROVISIONAL':
        # Preserve R57's explicit 1h/4h tactical authority. Those candidates
        # have already passed the execution router and were introduced to stop
        # stale senior bias from suppressing a fresh tactical trade. R59's
        # stricter maturity rule targets unpromoted provisional ideas, especially
        # the historically high-churn 5m bucket.
        r57_promoted=bool(
            (row or {}).get('_r56_trigger_selected')
            and (row or {}).get('_r57_trigger_score')
            and m['horizon'] in ('1h','4h')
        )
        if (not r57_promoted
                and not (m['hstate'] in ('BUILDING_TREND','CONFIRMED_TREND')
                         and m['hscore']>=0.65 and m['independent']>=4
                         and m['rr']>=max(1.50,rr_floor))):
            blockers.append('R59_PROVISIONAL_SETUP_NOT_MATURE')
    if (m['direction'] in ('LONG','SHORT') and m['hdir'] in ('LONG','SHORT')
            and m['hdir']!=m['direction']
            and m['hstate'] in ('BUILDING_TREND','CONFIRMED_TREND')
            and not _v90r59_strong_reversal(row,m)):
        blockers.append('R59_EXECUTION_TF_DIRECTION_CONFLICT')
    if (m['horizon']=='5m' and bool((row or {}).get('_r57_senior_conflict'))
            and not _v90r59_strong_reversal(row,m)):
        blockers.append('R59_5M_COUNTER_SENIOR_NOT_CONFIRMED')
    return {'eligible':not blockers,'blockers':blockers,
            'rr_floor':rr_floor,'move_floor':move_floor,**m}

def _signal_first_admission(row,policy,drawdown):
    out=dict(_v90r59_base_admission(row,policy,drawdown) or {})
    if not out.get('open'):
        return out
    q=_v90r59_quality_gate(row,policy)
    if not q.get('eligible'):
        return {'open':False,'fraction':0.0,'hard_veto':True,
                'reason':q['blockers'][0],'r59_quality_gate':q}
    out['r59_quality_gate']=q
    return out

def _v90r56_stop_noise_floor(row,horizon=None):
    base=float(_v90r59_base_stop_noise_floor(row,horizon))
    row=row or {}
    h=str(horizon or row.get('_r56_management_horizon') or row.get('horizon') or '')
    gate=((row.get('trade_plan') or {}).get('final_economics_gate') or {})
    cost=max(
      _v90r59_float(gate.get('modeled_round_trip_cost_pct'),0.0),
      _v90r59_float(VX.round_trip_cost_pct(row.get('spread_bps')),0.0))
    return max(base,{'5m':1.25,'1h':1.15,'4h':1.00}.get(h,1.00)*cost)

def _v90r59_dt(v):
    if not v:
        return None
    if isinstance(v,datetime):
        d=v
    else:
        try:
            d=datetime.fromisoformat(str(v).replace('Z','+00:00'))
        except Exception:
            return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)

def _v90r59_recent_opposite_exit(c,name,asset,direction,row,ts):
    try:
        last=c.execute(
          """SELECT direction,closed_at,payload FROM paper_trades
             WHERE portfolio_name=%s AND asset=%s AND status='CLOSED'
               AND closed_at IS NOT NULL
             ORDER BY closed_at DESC LIMIT 1""",(name,asset)).fetchone()
    except Exception:
        last=None
    if not last or str(last.get('direction') or '')==str(direction):
        return None
    closed=_v90r59_dt(last.get('closed_at'))
    now=_v90r59_dt(ts) or datetime.now(timezone.utc)
    if not closed:
        return {'blocked':True,'reason':'R59_OPPOSITE_EXIT_TIME_UNKNOWN'}
    age=max(0.0,(now-closed).total_seconds())
    if age>=600.0:
        return None
    observed=_v90r59_dt((row or {}).get('market_observed_at') or (row or {}).get('observed_at'))
    fresh=bool(observed and observed>closed)
    strong=_v90r59_strong_reversal(row)
    if fresh and strong:
        return None
    return {'blocked':True,'reason':'R59_WAIT_FRESH_OPPOSITE_EVENT',
            'age_seconds':age,'fresh_after_exit':fresh,'strong_reversal':strong}

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    existing=c.execute(
      "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
      (name,asset)).fetchone()
    if not existing:
        policy=POLICIES.get(str(name),{})
        actual=VX.entry_gate(row,price,direction,target_fraction)
        q=_v90r59_quality_gate(row,policy)
        actual_rr=_v90r59_float(actual.get('expected_to_stop_ratio'),0.0)
        actual_move=abs(_v90r59_float(actual.get('expected_move_pct'),0.0))
        actual_cost=_v90r59_float(actual.get('modeled_round_trip_cost_pct'),q.get('cost') or 0.0)
        rr_floor,move_floor=_v90r59_thresholds(
            policy,str((row or {}).get('horizon') or ''),actual_cost)
        blockers=list(actual.get('blockers') or [])
        if actual_rr<rr_floor:
            blockers.append('R59_FINAL_FILL_RR_MARGIN_TOO_LOW')
        if actual_move<move_floor:
            blockers.append('R59_FINAL_FILL_MOVE_MARGIN_TOO_LOW')
        blockers=list(dict.fromkeys(blockers))
        if (not actual.get('eligible')) or blockers:
            _record_entry_outcome(row,'BLOCKED',blockers[0] if blockers else 'R59_FINAL_FILL_GATE',blockers=blockers)
            print(json.dumps({
              'event':'V90_R59_ENTRY_BLOCKED','portfolio':name,'asset':asset,
              'direction':direction,'reason':blockers[0] if blockers else 'R59_FINAL_FILL_GATE',
              'blockers':blockers,'actual_rr':actual_rr,'rr_floor':rr_floor,
              'actual_move':actual_move,'move_floor':move_floor,'modeled_cost':actual_cost,
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0
        opposite=_v90r59_recent_opposite_exit(c,name,asset,direction,row,ts)
        if opposite and opposite.get('blocked'):
            _record_entry_outcome(row,'BLOCKED',opposite.get('reason') or 'RECENT_OPPOSITE_EXIT')
            print(json.dumps({
              'event':'V90_R59_ENTRY_BLOCKED','portfolio':name,'asset':asset,
              'direction':direction,**opposite,
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0
    return _v90r59_base_open_or_add(
      c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason)

def _v90r59_near_flat_flip_should_wait(z,price,ts,reason):
    if str(reason or '') not in ('V842_CONFIRMED_DIRECTION_FLIP','STRUCTURE_BREAK_EXIT_TO_CASH'):
        return False
    z=dict(z or {}); p=_v90j_json(z.get('payload'))
    entry=_v90r59_float(z.get('avg_entry_price') or p.get('entry_price'),0.0)
    px=_v90r59_float(price,0.0); d=str(z.get('direction') or '')
    if entry<=0 or px<=0 or d not in ('LONG','SHORT'):
        return False
    signed=(px/entry-1.0) if d=='LONG' else (entry/px-1.0)
    flat_band=max(0.0015,1.50*(2.0*float(COMMISSION)))
    if abs(signed)>=flat_band:
        return False
    now=_v90r59_dt(ts) or datetime.now(timezone.utc)
    previous_reason=str(p.get('r59_pending_flip_reason') or '')
    previous_at=_v90r59_dt(p.get('r59_pending_flip_at'))
    count=int(p.get('r59_pending_flip_count') or 0)
    if previous_reason==str(reason) and previous_at:
        age=max(0.0,(now-previous_at).total_seconds())
        if age<=600.0 and count>=1:
            return False
    return True

def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    if str(reason).startswith(('TAKE_PROFIT','DYNAMIC_PARTIAL_PROFIT')):
        trade=c.execute('SELECT gross_pnl_rub,fees_rub,funding_rub FROM paper_trades WHERE trade_id=%s',
                        (z.get('active_trade_id'),)).fetchone()
        quote=dict(VPG.exit_execution_quote(z,ts),price=price)
        quote.setdefault('observed_at',ts)
        profit=VPG.profit_exit_assessment(z,quote,trade,nav,COMMISSION)
        if not profit['eligible']:
            return 0.0
    payload=_v90j_json((z or {}).get('payload'))
    if str(payload.get('r66_event_id','')).startswith('R69_'):
        soft=any(k in str(reason) for k in ('EDGE_DECAY','LOW_PROB','CONFIDENCE','SIGNAL_WEAK'))
        if soft:return 0.0
    if (float(target_fraction or 0.0)<=0.0
            and _v90r59_near_flat_flip_should_wait(z,price,ts,reason)):
        patch={'r59_pending_flip_reason':str(reason),
               'r59_pending_flip_at':_v90j_iso(ts),'r59_pending_flip_count':1,
               'r59_flip_debounce':'ONE_CONFIRMATION_CYCLE_WHILE_NEAR_FLAT'}
        try:
            tid=(z or {}).get('active_trade_id')
            c.execute(
              "UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
              "WHERE portfolio_name=%s AND asset=%s",
              (json.dumps(patch,ensure_ascii=False),name,(z or {}).get('asset')))
            if tid:
                c.execute(
                  "UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                  "WHERE trade_id=%s",(json.dumps(patch,ensure_ascii=False),tid))
        except Exception:
            pass
        print(json.dumps({
          'event':'V90_R59_NEAR_FLAT_FLIP_DEBOUNCED','portfolio':name,
          'asset':(z or {}).get('asset'),'direction':(z or {}).get('direction'),
          'reason':reason,'price':price,
        },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
        return 0.0
    return _v90r59_base_close_or_reduce(
      c,p,name,z,price,target_fraction,nav,ts,reason)

def report(pg_connect):
    d=dict(_v90r59_base_report(pg_connect) or {})
    d['loss_root_cause_gate_r59']={
      'status':'ACTIVE','started_at':V90_R59_STARTED_AT,
      'post_cost_rr_floors':{
        'Impulse':'1.30 (+0.10 on 5m)','Aggressive':'1.35 (+0.10 on 5m)',
        'Champion':'1.45 (+0.10 on 5m)','Challenger':'1.55 (+0.10 on 5m)'},
      'minimum_expected_move':f'max({100*VX.MIN_EXPECTED_MOVE_PCT:.2f}%, {VX.MIN_MOVE_COST_MULTIPLE:.2f}x modeled round-trip cost)',
      'provisional_setup_requires_mature_structure':True,
      'execution_tf_direction_conflict_requires_strong_reversal':True,
      'five_minute_counter_senior_requires_strong_reversal':True,
      'stop_noise_floor_includes_modeled_friction':True,
      'near_flat_signal_flip_requires_second_confirmation_cycle':True,
      'opposite_reentry_requires_fresh_post_exit_event':True,
      'aggressive_qualified_initial_size_policy':'50%-100% unchanged after R59 quality admission',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),36)


# VERITAS V90 AGGRESSIVE INITIAL EXPOSURE AUTHORITY R61
_v90r61_base_admission=_signal_first_admission

def _v90r61_aggressive_initial_floor(row,policy,drawdown,out):
    row=row or {}; policy=policy or {}; out=dict(out or {})
    if str(policy.get('mode') or '')!='AGGRESSIVE' or not out.get('open'):
        return out
    direction=str(row.get('research_decision') or '')
    if direction not in ('LONG','SHORT'):
        return out
    m=_v90r59_entry_metrics(row)
    supporting=set(row.get('_supporting_horizons') or [])
    try: align=int(row.get('_alignment_count') or len(supporting))
    except Exception: align=len(supporting)
    try: conf=float(row.get('confidence') or row.get('_pwin') or 0.0)
    except Exception: conf=0.0
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    super_sig=tier in ('SUPER_LONG','SUPER_SHORT') or bool(row.get('_r20_super_priority'))

    h=str(row.get('horizon') or '')
    floor=0.50
    # Canonical initial ladder. A 5m SUPER is not automatically 100%:
    # 5m alone=50%, +1h=75%, +1h+4h=100%.
    if h=='5m':
        if '1h' in supporting:
            floor=0.75
        if '1h' in supporting and '4h' in supporting and super_sig:
            floor=1.00
    elif h=='1h':
        # A valid 1h tactical reversal starts at 50%; scale is earned later.
        floor=0.50
    elif (super_sig or (conf>=0.82 and int(m.get('independent') or 0)>=5
                        and float(m.get('rr') or 0.0)>=1.50 and align>=3)):
        floor=1.00
    elif (conf>=0.72 and int(m.get('independent') or 0)>=4
          and float(m.get('rr') or 0.0)>=1.30):
        floor=0.75

    rg=_risk_governor(drawdown)
    mult=max(0.0,min(1.0,float(rg.get('multiplier') or 0.0)))
    if not rg.get('new_risk',True):
        return {**out,'open':False,'fraction':0.0,'reason':'R61_RISK_GOVERNOR_BLOCK'}
    floor*=mult

    try: risk_cap=_v90r24_stop_risk_cap(row)
    except Exception: risk_cap=None
    target=max(float(out.get('fraction') or 0.0),floor)
    if risk_cap is not None:
        target=min(target,float(risk_cap))
    target=_clip(_round_step(target),0.0,float(policy.get('max_fraction') or 5.0))
    out['fraction']=target
    out['open']=bool(target>0)
    out['r61_aggressive_initial']={
      'requested_floor':floor,'final_fraction':target,'risk_cap_fraction':risk_cap,
      'confidence':conf,'independent':int(m.get('independent') or 0),
      'rr':float(m.get('rr') or 0.0),'alignment':align,'super':super_sig,
      'risk_multiplier':mult,
    }
    return out

def _signal_first_admission(row,policy,drawdown):
    out=dict(_v90r61_base_admission(row,policy,drawdown) or {})
    return _v90r61_aggressive_initial_floor(row,policy,drawdown,out)


# VERITAS V90 INDEPENDENT EPISODE / STABLE SETUP ID R60
_v90r60_base_report=report

def report(pg_connect):
    d=dict(_v90r60_base_report(pg_connect) or {})
    try:
        learn=_v90r29_refresh(pg_connect)
        summary=dict((learn or {}).get('summary') or {})
    except Exception as ex:
        summary={'status':'UNAVAILABLE','error':f'{type(ex).__name__}: {ex}'[:180]}
    d['independent_episode_learning_r60']={
      'status':'ACTIVE' if not summary.get('error') else 'DEGRADED',
      'stable_setup_identity':True,
      'current_price_can_create_new_setup_id':False,
      'moving_stop_can_create_new_setup_id':False,
      'one_market_idea_one_learning_episode':True,
      'independent_episode_policy':summary.get('independent_episode_policy')
          or 'ONE_MARKET_IDEA_ONE_LEARNING_EPISODE',
      'eligible_independent_episodes':summary.get('eligible_episodes'),
      'duplicates_excluded_last_sanitize':summary.get('duplicate_market_episodes_excluded',0),
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),37)


# VERITAS V90 NQ TREND EXECUTION CONTRACT R64
# Root-cause repair for 2026-10-01 missed NQ trend:
# 1) admission economics use the full runner/thesis target; TP1 remains a partial
#    management exit and must not collapse reward/risk before the trade exists;
# 2) historical losses attributed mainly to stop/exit management do not veto a
#    fresh, independently confirmed NQ trend. They reduce size instead.
V90_R64_STARTED_AT=os.getenv('VERITAS_R64_EPOCH','2026-10-01T17:30:00+00:00')
_v90r64_base_prepare_entry_row=_v90r56_prepare_entry_row
_v90r64_base_candidate_guard=_v90_candidate_profit_guard
_v90r64_base_report=report

def _v90r56_prepare_entry_row(row):
    original=dict(row or {})
    original_plan=dict(original.get('trade_plan') or {})
    x=dict(_v90r64_base_prepare_entry_row(original) or {})
    plan=dict(x.get('trade_plan') or {})
    tp=dict(x.get('_r56_tp_plan') or {})
    d=_v90r56_direction(original)
    try:
        px=float(original.get('price') or 0.0)
        runner=float(original_plan.get('target_price') or 0.0)
        stop=float(plan.get('stop_price') or 0.0)
        original_expected=abs(float(original_plan.get('expected_move_pct') or 0.0))
    except Exception:
        px=runner=stop=original_expected=0.0
    runner_valid=bool(
        px>0 and runner>0 and stop>0 and d in ('LONG','SHORT')
        and ((d=='LONG' and runner>px>stop) or (d=='SHORT' and runner<px<stop))
    )
    if runner_valid and tp.get('tp1_price'):
        runner_move=abs(runner/px-1.0)
        expected=runner_move if original_expected<=0 else min(runner_move,original_expected)
        stop_dist=abs(stop/px-1.0)
        if expected>0 and stop_dist>0:
            # The entry is justified by the total trade thesis. TP1 is a partial
            # harvest level; the remaining runner continues toward this target.
            plan['target_price']=runner
            plan['expected_move_pct']=expected
            plan['expected_to_stop_ratio']=expected/max(stop_dist,1e-9)
            plan['r64_admission_target_price']=runner
            plan['r64_admission_target_method']='RUNNER_THESIS_TARGET'
            plan['r64_partial_tp1_price']=tp.get('tp1_price')
            plan['r64_partial_tp1_move_pct']=tp.get('tp1_move_pct')
            plan['r64_partial_tp1_not_final_target']=True
            x['trade_plan']=plan
            x['_r64_runner_economics']=True
    return VTE.prepare_row(x)

def _v90r64_nq_management_recovery(row,policy,economics,guard):
    row=row or {}; policy=policy or {}; economics=economics or {}; guard=dict(guard or {})
    if str(row.get('asset') or '')!='NQ':
        return guard
    direction=str(row.get('research_decision') or '')
    horizon=str(row.get('horizon') or '')
    if direction not in ('LONG','SHORT') or horizon not in ('5m','1h'):
        return guard
    regime=str(row.get('regime') or '')
    if regime.startswith('RANGE_'):
        return guard
    attr=dict(guard.get('learning_attribution') or {})
    if not attr.get('management_dominated') or attr.get('negative_expectancy_is_directional'):
        return guard
    try:
        entry_error=float(attr.get('entry_error_rate') or 0.0)
        rr=float(economics.get('expected_to_stop_ratio') or 0.0)
        move=abs(float(economics.get('expected_move_pct') or 0.0))
        cost=abs(float(economics.get('modeled_round_trip_cost_pct') or 0.0))
        hscore=float(guard.get('horizon_structure_score') or
                     ((row.get('horizon_structure') or {}).get('score')) or 0.0)
        indep=int(guard.get('independent') or 0)
        alignment=int(guard.get('alignment_count') or 0)
    except Exception:
        return guard
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    entry_quality=str(row.get('entry_quality') or (row.get('trade_plan') or {}).get('entry_quality') or '')
    strong_current=bool(
        rr>=1.60 and move>=max(0.008,4.0*cost)
        and hscore>=0.65 and indep>=4 and alignment>=2
        and entry_error<=0.20
        and (tier in ('SUPER_LONG','SUPER_SHORT')
             or entry_quality in ('FRESH_BREAKOUT','CONFIRMED_TREND'))
    )
    if not strong_current:
        return guard

    # Only historical/management-derived vetoes are softened. Current quote,
    # current economics, direction conflicts and hard invalidation are untouched.
    soft_codes={
      'LEARNED_CALIBRATED_RR_TOO_LOW',
      'LEARNED_EARLY_BREAKOUT_EDGE_TOO_SMALL',
      'LEARNED_COST_DRAG_CLUSTER',
      'LEARNED_NEGATIVE_CONTEXT_EXPECTANCY',
      'EARLY_BREAKOUT_WAIT_CONFIRMATION',
    }
    # Challenger normally needs 3-TF alignment; a fresh NQ 5m+1h trend may take
    # only a small probe while the third timeframe is still forming.
    if alignment>=2:
        soft_codes.add('INSUFFICIENT_MULTI_TF_ALIGNMENT')

    blockers=list(guard.get('blockers') or [])
    softened=[b for b in blockers if b in soft_codes]
    if not softened:
        return guard
    blockers=[b for b in blockers if b not in soft_codes]
    warnings=list(guard.get('soft_warnings') or [])+[
        'R64_NQ_STRONG_TREND_MANAGEMENT_HISTORY_SOFTENED:'+b for b in softened
    ]
    mode=str(policy.get('mode') or 'CORE')
    cap={'IMPULSE_ONLY':0.10,'AGGRESSIVE':0.50,'CORE':0.05,'CHALLENGER':0.05}.get(mode,0.05)
    oldcap=guard.get('size_cap')
    if oldcap is not None:
        try: cap=min(cap,float(oldcap))
        except Exception: pass
    guard.update({
      'blockers':list(dict.fromkeys(blockers)),
      'soft_warnings':list(dict.fromkeys(warnings)),
      'eligible':not blockers,
      'status':'PASS' if not blockers else 'BLOCK',
      'size_cap':cap,
      'size_multiplier':min(float(guard.get('size_multiplier') or 1.0),0.75),
      'r64_nq_management_recovery':{
        'active':True,'softened_blockers':softened,
        'current_rr':rr,'current_expected_move_pct':move,
        'current_cost_pct':cost,'horizon_structure_score':hscore,
        'independent':indep,'alignment_count':alignment,
        'entry_error_rate':entry_error,
        'principle':'current strong trend may probe; old management errors cannot veto direction',
      },
    })
    return guard

def _v90_candidate_profit_guard(row,policy,economics):
    base=dict(_v90r64_base_candidate_guard(row,policy,economics) or {})
    return _v90r64_nq_management_recovery(row,policy,economics,base)

def report(pg_connect):
    d=dict(_v90r64_base_report(pg_connect) or {})
    d['nq_trend_execution_contract_r64']={
      'status':'ACTIVE','started_at':V90_R64_STARTED_AT,
      'runner_target_authoritative_for_entry_economics':True,
      'tp1_role':'partial_profit_harvest_not_final_reward',
      'management_dominated_history_can_veto_direction':False,
      'strong_nq_recovery_requires':{
        'horizons':['5m','1h'],'raw_post_cost_rr_min':1.60,
        'expected_move_min':'max(0.8%, 4x current modeled cost)',
        'horizon_structure_score_min':0.65,'independent_evidence_min':4,
        'alignment_min':2,'entry_error_rate_max':0.20,
      },
      'hard_current_economics_still_authoritative':True,
      'freshness_still_authoritative':True,
      'incident_reference':'NQ 2026-10-01 17:33-17:35 UTC SUPER_LONG/PASS not executed',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),38)


# VERITAS V90 CRYPTO EARLY CAPTURE / ANTI-CHASE R65
# Repairs the 2026-10-02 BTC/ETH pattern: the engine saw the impulse early,
# historical soft vetoes delayed execution, and Core/Impulse later entered
# after most of the move had already been consumed.
V90_R65_STARTED_AT=os.getenv('VERITAS_R65_EPOCH','2026-10-02T06:00:00+00:00')
_v90r65_base_transition_book=_v90_trend_transition_candidate_book
_v90r65_base_admission=_signal_first_admission
_v90r65_base_open_or_add=_open_or_add
_v90r65_base_trailing_apply=_v90tr_apply
_v90r65_base_report=report
_R65_CRYPTO={'BTC','ETH'}
_R65_ALLOWED_CURRENT_ECON={'NET_REWARD_RISK_BELOW_FLOOR'}

def _v90r65_num(v,default=0.0):
    try:
        x=float(v)
        return x if math.isfinite(x) else float(default)
    except Exception:
        return float(default)

def _v90r65_genesis_metrics(row):
    row=dict(row or {})
    asset=str(row.get('asset') or '')
    h=str(row.get('horizon') or '')
    d=_v90r56_direction(row)
    if asset not in _R65_CRYPTO or h not in _R56_TRIGGER_HORIZONS or d not in ('LONG','SHORT'):
        return {'eligible':False,'reason':'R65_SCOPE'}
    if _v90r55_invalidated(row) or not bool(row.get('source_gate_pass',True)) or not bool(row.get('market_open',True)):
        return {'eligible':False,'reason':'R65_HARD_OR_SOURCE_VETO'}
    plan=dict(row.get('trade_plan') or {})
    gate=dict(plan.get('final_economics_gate') or {})
    blockers=list(gate.get('blockers') or [])
    hard=[b for b in blockers if b not in _R65_ALLOWED_CURRENT_ECON]
    if hard:
        return {'eligible':False,'reason':'R65_CURRENT_ECONOMICS_VETO','blockers':blockers}
    reason=str(plan.get('reason') or '')
    if 'rule_arbitration_veto:' in reason and 'NEGATIVE_VALIDATED_SETUP_EDGE' not in reason:
        return {'eligible':False,'reason':'R65_CURRENT_RULE_VETO'}
    hs=dict(row.get('horizon_structure') or {})
    hscore=_v90r65_num(hs.get('score') or row.get('horizon_structure_score'))
    hstate=str(hs.get('state') or row.get('horizon_structure_state') or '')
    indep=int(_v90r65_num(
        (((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'))
        or row.get('independent_evidence_families'),0))
    conf=_v90r65_num(row.get('confidence') or row.get('_pwin'))
    rr=_v90r65_num(plan.get('expected_to_stop_ratio') or row.get('expected_to_stop_ratio'))
    move=abs(_v90r65_num(plan.get('expected_move_pct') or row.get('expected_move_pct')))
    cost=abs(_v90r65_num(gate.get('modeled_round_trip_cost_pct')))
    quality=str(row.get('entry_quality') or plan.get('entry_quality') or '')
    tier=str(row.get('signal_tier') or row.get('execution_signal_tier') or '')
    hmin={'5m':.58,'1h':.55,'4h':.65}.get(h,.65)
    rrmin={'5m':1.35,'1h':1.45,'4h':1.55}.get(h,1.55)
    movemin=VX.minimum_expected_move_pct(cost)
    quality_ok=quality in ('FRESH_BREAKOUT','CONFIRMED_TREND','NEW_SETUP_PROVISIONAL')
    trend_ok=hstate in ('BUILDING_TREND','CONFIRMED_TREND') or tier in ('SUPER_LONG','SUPER_SHORT')
    timing=_v90r56_late_entry_gate(row)
    eligible=bool(conf>=.72 and hscore>=hmin and indep>=3 and rr>=rrmin
                  and move>=movemin and quality_ok and trend_ok and timing.get('eligible'))
    score=(3.6 if h=='5m' else 3.3 if h=='1h' else 3.0)+hscore+0.50*conf+0.08*min(indep,6)+0.12*min(rr,3.0)
    return {
      'eligible':eligible,'reason':'R65_CRYPTO_TREND_GENESIS' if eligible else 'R65_GENESIS_QUALITY_WAIT',
      'asset':asset,'horizon':h,'direction':d,'confidence':conf,'hscore':hscore,
      'hstate':hstate,'independent':indep,'rr':rr,'expected_move_pct':move,
      'modeled_cost_pct':cost,'entry_quality':quality,'signal_tier':tier,
      'current_economics_blockers':blockers,'timing':timing,'score':score,
      'marginal_economics':bool(blockers),
    }

def _v90r65_best_crypto_genesis(summary,asset):
    rows=[]
    for r0 in summary or []:
        if str((r0 or {}).get('asset') or '')!=str(asset):
            continue
        m=_v90r65_genesis_metrics(r0)
        if m.get('eligible'):
            x=dict(r0); x['_r65_crypto_genesis']=m
            x['_r56_trigger_selected']=True
            x['_r56_entry_horizon']=x.get('horizon')
            x['_r56_management_horizon']=x.get('horizon')
            # R65.2: preserve the base order-journal probability contract.
            _p,_ps=_signal_probability(x)
            x['_pwin']=float(_p)
            x['_pwin_source']='R65_CRYPTO_GENESIS_'+str(_ps)
            x['_rank']=max(float(x.get('_rank') or 0.0),float(m.get('score') or 0.0))
            rows.append((float(m.get('score') or 0.0),x))
    if not rows:
        return None
    rows.sort(key=lambda z:z[0],reverse=True)
    return rows[0][1]

def _v90r65_execution_timing(row,price,direction,ts):
    """Validate new crypto risk at the executable price, including adds.

    A senior thesis is not an entry trigger. A neutral tactical row is allowed;
    an observed local reversal is not overridden by an older hourly trend.
    """
    row=dict(row or {})
    if str(row.get('horizon') or '') not in _R56_TRIGGER_HORIZONS:
        return {'eligible':False,'reason':'R65_LOCAL_ENTRY_TRIGGER_REQUIRED'}
    now=_v90r55_dt(ts) or datetime.now(timezone.utc)
    quote=row.get('_execution_quote') or {}
    observed=quote.get('observed_at') or row.get('market_observed_at') or row.get('observed_at')
    fresh=VPG.quote_gate(observed,now=now,execution=True,asset=row.get('asset'))
    if not fresh.get('eligible'):
        return {'eligible':False,'reason':'R65_ENTRY_QUOTE_NOT_FRESH','quote_gate':fresh}
    tactical=row.get('_r65_tactical_context')
    if row.get('horizon') in ('1m','5m'):
        tactical=row
    if not tactical:
        return {'eligible':False,'reason':'R65_LOCAL_ENTRY_CONTEXT_REQUIRED'}
    local_fresh=VPG.quote_gate(tactical.get('market_observed_at') or tactical.get('observed_at'),
                             now=now,protective=True)
    if not local_fresh.get('eligible'):
        return {'eligible':False,'reason':'R65_LOCAL_ENTRY_CONTEXT_STALE'}
    hs=tactical.get('horizon_structure') or {}
    local_direction=str(hs.get('direction') or '')
    reversal=str(hs.get('state') or '')=='EXIT_REVERSAL'
    opposed=bool(local_direction in ('LONG','SHORT') and local_direction!=direction
                 and str(hs.get('state') or '') in ('BUILDING_TREND','CONFIRMED_TREND'))
    if reversal or opposed:
        return {'eligible':False,'reason':'R65_WAIT_LOCAL_REVERSAL',
                'local_state':hs.get('state'),'local_direction':local_direction}
    side='BUY' if direction=='LONG' else 'SELL_SHORT'
    # Use the most recent tactical book; never execute from an old hourly book.
    fill=VX.simulated_fill(row.get('asset'),side,price,
                          row.get('_r65_order_fraction',.10),
                          bid=quote.get('best_bid') or tactical.get('best_bid'),
                          ask=quote.get('best_ask') or tactical.get('best_ask'))
    if (row.get('_r66_add_decision') or {}).get('eligible'):
        ctx=VTE.context_of(row);anchor=VTE.number(ctx.get('last_close'),0)
        atr=VTE.number(ctx.get('atr'),0)
        displacement=abs(fill['fill_price']-anchor)/atr if atr>0 else float('inf')
        timing={'eligible':displacement<=.8,'reason':'R66_ADD_CONFIRMED' if displacement<=.8 else 'R66_WAIT_RETEST',
                'confirmation_displacement_atr':displacement}
    else:
        timing=_v90r56_late_entry_gate(row,fill['fill_price'])
    return {**timing,'execution_model':fill,'local_state':hs.get('state')}

def _v90_trend_transition_candidate_book(summary,core_candidates,mode=None):
    out=dict(_v90r65_base_transition_book(summary,core_candidates,mode) or {})
    # A cancelled high-rank scenario must not mask a valid same-direction
    # local trigger. The replacement still passes every portfolio/fill gate.
    for asset,selected in list(out.items()):
        if _v90r55_invalidated(selected):
            trigger=_v90r56_trigger_row(summary,asset,_v90r56_direction(selected))
            if trigger:
                trigger=dict(trigger)
                trigger['_pwin'],trigger['_pwin_source']=_signal_probability(trigger)
                out[asset]=trigger
    for asset in _R65_CRYPTO:
        g=_v90r65_best_crypto_genesis(summary,asset)
        old=out.get(asset)
        if g and (old is None or str(old.get('research_decision') or '')==str(g.get('research_decision') or '')):
            out[asset]=g
        selected=out.get(asset)
        if not selected:
            continue
        # Senior horizons retain their thesis role, but must route an order
        # through an actual local trigger in every portfolio.
        if selected.get('horizon') in _R56_SENIOR_HORIZONS:
            trigger=_v90r56_trigger_row(summary,asset,_v90r56_direction(selected))
            if trigger:
                selected={**trigger,'_r56_thesis_horizon':selected.get('horizon')}
                selected['_pwin'],selected['_pwin_source']=_signal_probability(trigger)
        selected=dict(selected)
        local=[r for r in summary or [] if r.get('asset')==asset and r.get('horizon')=='5m']
        if local:
            selected['_r65_tactical_context']=dict(max(local,key=lambda r:str(
                r.get('market_observed_at') or r.get('observed_at') or '')))
        out[asset]=selected
    # A published fresh local event is an independent entry candidate, including
    # against a senior forecast. Its own direction, stop and target remain intact;
    # every admission/fill gate still runs before any paper position can open.
    for r0 in summary or []:
        if r0.get('horizon') not in ('1m','5m') or _v90r55_invalidated(r0):continue
        asset=r0.get('asset');direction=r0.get('research_decision')
        event=VTE.context_of(r0).get('event') or {}
        if direction not in ('LONG','SHORT') or event.get('direction')!=direction:continue
        old=out.get(asset)
        if (old and old.get('horizon')=='1m' and r0.get('horizon')=='5m'
                and VTE.event_gate(old,old.get('price'),old.get('research_decision'),
                                   datetime.now(timezone.utc)).get('eligible')):continue
        ready=VTE.event_gate(r0,r0.get('price'),direction,datetime.now(timezone.utc))
        if not ready.get('eligible'):continue
        selected=VTE.prepare_row(r0)
        selected['_pwin'],selected['_pwin_source']=_signal_probability(selected)
        selected['_r56_trigger_selected']=True
        selected['_r56_entry_horizon']=r0.get('horizon');selected['_r56_management_horizon']='5m'
        selected['_r65_tactical_context']=dict(r0)
        selected['_r66_closed_trigger']=ready
        if old:
            selected['_r56_thesis_horizon']=old.get('horizon')
            selected['_r77_prior_candidate_direction']=old.get('research_decision')
        out[asset]=selected
    return out

def _v90r65_genesis_fraction(row,policy,drawdown):
    m=dict((row or {}).get('_r65_crypto_genesis') or {})
    mode=str((policy or {}).get('mode') or 'CORE')
    conf=float(m.get('confidence') or 0.0)
    hscore=float(m.get('hscore') or 0.0)
    marginal=bool(m.get('marginal_economics'))
    if mode=='AGGRESSIVE':
        f=.50
        if not marginal and conf>=.80 and hscore>=.80:
            f=.75
    elif mode=='IMPULSE_ONLY':
        f=.10
    elif mode=='CORE':
        f=.05 if marginal or str(m.get('horizon'))=='5m' else .10
    else:
        if conf<.78 or int(m.get('independent') or 0)<4:
            return 0.0
        f=.05
    # Compute stop-risk cap from the CURRENT rebased signal geometry only.
    # Do not inherit a zero/invalid cap from the parent setup.
    try:
        px=float((row or {}).get('price') or 0.0)
        stop=float(((row or {}).get('trade_plan') or {}).get('stop_price') or 0.0)
        rp=abs(px-stop)/px if px>0 and stop>0 else 0.0
        if rp>0:
            f=min(f,float(MAX_STOP_RISK_NAV)/rp)
        else:
            return 0.0
    except Exception:
        return 0.0
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return 0.0
    f*=float(rg.get('multiplier') or 0.0)
    return _clip(_round_step(f),0.0,float((policy or {}).get('max_fraction') or 2.0))

def _signal_first_admission(row,policy,drawdown):
    # Quote refresh can precede admission; its timestamp must never validate
    # economics or distance computed from an older signal price.
    row=dict(row or {})
    q=row.get('_execution_quote') or {}
    row['price']=VTE.number(q.get('price'),row.get('price'))
    row=VTE.prepare_row(row)
    event=VTE.event_gate(row,row.get('price'),row.get('research_decision'),datetime.now(timezone.utc))
    if not event.get('eligible'):
        return {'open':False,'fraction':0.,'hard_veto':True,'reason':event['reason'],'trend_event':event}
    g=(row.get('trade_plan') or {}).get('r66_geometry') or {}
    if g.get('reason')=='R66_SENIOR_BREAK_NOT_HELD':
        return {'open':False,'fraction':0.0,'hard_veto':True,'reason':g['reason'],'r66_geometry':g}
    ev=VTE.context_of(row).get('event') or {}
    if str(ev.get('event_id','')).startswith('R69_'):
        from veritas_minute_entry import structural_fraction
        rg=_risk_governor(drawdown)
        cap=min(float(policy.get('max_fraction') or 1.),float(_v90r24_stop_risk_cap(row) or 0.))
        f=structural_fraction(row,policy.get('mode'),cap)
        f=math.floor(f*float(rg.get('multiplier') or 0.)/.05+1e-9)*.05
        economics=VX.entry_gate(row,row.get('price'),row.get('research_decision'),f)
        plan=row.get('trade_plan') or {}
        history=plan.get('profitability_gate') or {}
        hard=bool((plan.get('trade_integrity') or {}).get('hard_invalidation')
                  or (plan.get('trade_integrity') or {}).get('fast_tf_conflict')
                  or (plan.get('rule_arbitration') or {}).get('hard_veto')
                  or (plan.get('reentry_intelligence') or {}).get('allowed') is False
                  or (history.get('allow') is False and history.get('status')!='NOT_APPLICABLE')
                  or history.get('status')=='NEGATIVE_EDGE')
        ok=bool(f>0 and not hard and plan.get('eligible',True) and rg.get('new_risk') is not False and row.get('execution_eligible')
                and row.get('source_gate_pass') and economics.get('eligible'))

        # R78: verified fundamental catalyst = a new continuation setup, not a
        # resurrection of the spent breakout. If the normal fixed-R/R gate is
        # the ONLY economics veto, allow a deliberately small paper probe when
        # post-cost reward is still positive and current structure is unusually
        # strong. The existing scale engine may add only after a distinct new
        # structural confirmation. Source, quote freshness, stop risk, negative
        # historical edge and hard invalidations remain absolute vetoes.
        catalyst=bool(ev.get('catalyst_continuation') or
                      ev.get('event_type')=='CATALYST_CONTINUATION')
        econ_blockers=set(str(x) for x in (economics.get('blockers') or []))
        allowed_probe_blockers={'NET_REWARD_RISK_BELOW_FLOOR'}
        try: conf=float(row.get('confidence') or row.get('_pwin') or 0.0)
        except Exception: conf=0.0
        hs=row.get('horizon_structure') or {}
        try: hscore=float(hs.get('score') or row.get('horizon_structure_score') or 0.0)
        except Exception: hscore=0.0
        try:
            indep=int((((row.get('institutional_signal') or {}).get('evidence_independence') or {})
                       .get('independent_count')) or row.get('independent_evidence_families') or 0)
        except Exception:
            indep=0
        try: cstrength=float(((ev.get('catalyst') or {}).get('strength')) or 0.0)
        except Exception: cstrength=0.0
        try: net_reward=float(economics.get('net_reward_pct') or 0.0)
        except Exception: net_reward=0.0
        try: net_rr=float(economics.get('expected_to_stop_ratio') or 0.0)
        except Exception: net_rr=0.0
        qgate=economics.get('quote_time_gate') or {}
        catalyst_probe=bool(
            catalyst and not ok and f>0 and not hard
            and plan.get('eligible',True)
            and rg.get('new_risk') is not False
            and row.get('execution_eligible') and row.get('source_gate_pass')
            and bool(qgate.get('eligible',True))
            and econ_blockers and econ_blockers.issubset(allowed_probe_blockers)
            and net_reward>0 and net_rr>=0.45
            and conf>=0.75 and hscore>=0.78 and indep>=5 and cstrength>=0.85
        )
        if catalyst_probe:
            probe={'AGGRESSIVE':0.25,'IMPULSE_ONLY':0.15,'CORE':0.10,'CHALLENGER':0.10}.get(
                str((policy or {}).get('mode') or 'CORE'),0.10)
            f=min(float(f),float(probe))
            f=math.floor(f/.05+1e-9)*.05
            ok=bool(f>0)
        return {'open':ok,'fraction':f if ok else 0.,'hard_veto':not ok,
                'reason':('R78_CATALYST_CONTINUATION_PROBE' if catalyst_probe else
                          'R69_STRUCTURAL_EVENT' if ok else 'R69_SOURCE_RISK_OR_ECONOMICS'),
                'trend_event':event,'economics':economics,
                'economics_blockers':economics.get('blockers') or [],
                'quote_time_gate':economics.get('quote_time_gate'),
                'net_reward_risk':economics.get('expected_to_stop_ratio'),
                'profitability_gate':history,
                'catalyst_probe':catalyst_probe,
                'catalyst':ev.get('catalyst') if catalyst else None,
                'probability':None,'probability_source':'UNCALIBRATED_STRUCTURAL_RULE',
                'signal_score':row.get('confidence')}
    base=dict(_v90r65_base_admission(row,policy,drawdown) or {})
    m=dict((row or {}).get('_r65_crypto_genesis') or {})
    if not m.get('eligible'):
        return base
    timing=m.get('timing') or {}
    if not timing.get('eligible'):
        return {'open':False,'fraction':0.0,'hard_veto':True,
                'reason':'R65_WAIT_RETEST_NO_CHASE','r65_crypto_genesis':m}
    f=_v90r65_genesis_fraction(row,policy,drawdown)
    if f<=0:
        return base
    # Current source/hard economics stay authoritative. R65 softens only
    # management/history maturity and, for a tiny genesis probe, a lone
    # NET_REWARD_RISK_BELOW_FLOOR current blocker.
    base.update({
      'open':True,'fraction':max(f,float(base.get('fraction') or 0.0) if base.get('open') else 0.0),
      'reason':'R65_CRYPTO_TREND_GENESIS','hard_veto':False,
      'r65_crypto_genesis':m,'r65_early_capture':True,
    })
    return base

def _v90r65_actual_genesis_fill_ok(row,price,direction,target_fraction):
    actual=VX.entry_gate(row,price,direction,target_fraction)
    blockers=list(actual.get('blockers') or [])
    hard=[b for b in blockers if b not in _R65_ALLOWED_CURRENT_ECON]
    rr=_v90r65_num(actual.get('expected_to_stop_ratio'))
    move=abs(_v90r65_num(actual.get('expected_move_pct')))
    cost=abs(_v90r65_num(actual.get('modeled_round_trip_cost_pct'),.002))
    ok=bool(not hard and rr>=.95 and move>=VX.minimum_expected_move_pct(cost))
    return {'eligible':ok,'blockers':blockers,'hard_blockers':hard,
            'actual_rr':rr,'actual_move':move,'modeled_cost':cost}

def _r72_event_reentry_gate(c,name,asset,direction,event):
    event_id=(event or {}).get('event_id')
    if not event_id:
        return {'eligible':False,'reason':'R72_EVENT_ID_MISSING'}
    try:
        prior=c.execute("""SELECT trade_id FROM paper_trades
            WHERE portfolio_name=%s AND asset=%s AND direction=%s
              AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
              AND payload->>'r66_event_id'=%s LIMIT 1""",
            (name,asset,direction,event_id)).fetchone()
    except Exception:
        return {'eligible':False,'reason':'R72_EVENT_HISTORY_UNAVAILABLE'}
    return {'eligible':not bool(prior),'reason':'R72_EVENT_ALREADY_TRADED' if prior else 'R72_NEW_EVENT',
            'event_id':event_id}

def _open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    row=dict(row or {})
    existing=c.execute(
      "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
      (name,asset)).fetchone()
    row=VTE.prepare_row(row,price)
    quote=row.get('_execution_quote') or {}
    if quote:
        row['best_bid']=quote.get('best_bid');row['best_ask']=quote.get('best_ask')
    current=(abs(float(existing.get('units') or 0.0)*float(price))/max(float(nav),1.0)
             if existing and existing.get('direction')==direction else 0.0)
    increases_risk=bool(not existing or existing.get('direction')!=direction
                        or float(target_fraction)>current+.0025)
    if increases_risk:
        if VX.is_proxy_price(asset,quote or row):
            _record_entry_outcome(row,'BLOCKED','R67_DIRECT_NQ_QUOTE_REQUIRED')
            return 0.0
        context=VTE.context_gate(row,_v90r55_dt(ts))
        if not context['eligible']:
            _record_entry_outcome(row,'BLOCKED',context['reason'],context_freshness=context)
            return 0.0
        fresh=VPG.quote_gate(quote.get('observed_at') or row.get('market_observed_at') or row.get('observed_at'),
                            now=_v90r55_dt(ts),execution=True,asset=asset)
        if not fresh.get('eligible'):
            _record_entry_outcome(row,'BLOCKED','R66_EXECUTION_QUOTE_STALE',quote_gate=fresh)
            return 0.0
        if not existing or existing.get('direction')!=direction:
            fill=VX.simulated_fill(asset,'BUY' if direction=='LONG' else 'SELL_SHORT',price,
                                  target_fraction,bid=row.get('best_bid'),ask=row.get('best_ask'))
            # Use the observed quote for technical timing, exactly as the
            # planner and final entry gate do; fill costs remain in risk below.
            event=VTE.event_gate(row,price,direction,ts)
            if not event['eligible']:
                _record_entry_outcome(row,'BLOCKED',event['reason'],trend_event=event)
                return 0.0
            reuse=_r72_event_reentry_gate(c,name,asset,direction,VTE.context_of(row).get('event'))
            if not reuse['eligible']:
                _record_entry_outcome(row,'BLOCKED',reuse['reason'],event_reentry=reuse)
                return 0.0
            if VTE.context_of(row).get('local_breakout_required'):
                stop=VTE.number((row.get('trade_plan') or {}).get('stop_price'))
                risk=abs(float(fill['fill_price'])-stop)/float(fill['fill_price']) if stop else 0.
                costs=VX.economics_gate(asset,dict(row.get('trade_plan') or {},entry_price=price,
                    direction=direction,initial_position_fraction=target_fraction,horizon=row.get('horizon')))
                net_risk=max(risk+VX.round_trip_cost_pct(row.get('spread_bps')),
                             float(costs.get('net_risk_pct') or 0.))
                cap=MAX_STOP_RISK_NAV/net_risk if risk>0 else 0.
                target_fraction=min(float(target_fraction),math.floor(cap/.05)*.05)
                if target_fraction<=0:
                    _record_entry_outcome(row,'BLOCKED','R67_STRUCTURAL_STOP_RISK_LIMIT')
                    return 0.0
                row['_r65_entry_timing']=event
        elif asset=='NQ' and VTE.context_of(row).get('status')!='OK':
            _record_entry_outcome(row,'BLOCKED','R67_LOCAL_CONTEXT_REQUIRED')
            return 0.0
        elif existing.get('direction')==direction and VTE.context_of(row).get('status')=='OK':
            held=dict(existing);held['payload']=_v90j_json(existing.get('payload'))
            scale=VTE.scale_decision(held,row,price,target_fraction,nav,
                float(VX.round_trip_cost_pct(row.get('spread_bps'))),MAX_STOP_RISK_NAV)
            if not scale.get('eligible'):
                _record_entry_outcome(row,'BLOCKED',scale['reason'],scale=scale)
                return 0.0
            target_fraction=scale['fraction']
            row['_r66_add_decision']=scale
    if increases_risk and (asset in _R65_CRYPTO or
                          (not existing and str(row.get('horizon') or '') in _R56_TRIGGER_HORIZONS)):
        row['_r65_order_fraction']=max(0.0,float(target_fraction)-current)
        timing=(_v90r65_execution_timing(row,price,direction,ts) if asset in _R65_CRYPTO
                else _v90r56_late_entry_gate(row,price))
        if not timing.get('eligible'):
            _record_entry_outcome(row,'BLOCKED',timing.get('reason'),timing=timing)
            print(json.dumps({
              'event':'V90_R65_LATE_ENTRY_BLOCKED','portfolio':name,'asset':asset,
              'direction':direction,'price':price,**timing,
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0
        row['_r65_entry_timing']=timing
        if asset in _R65_CRYPTO:
            # Preserve the exact book used by the timing check in the fill lane.
            model=timing.get('execution_model') or {}
            row['best_bid']=model.get('bid'); row['best_ask']=model.get('ask')
    if not existing and (row.get('_r65_crypto_genesis') or {}).get('eligible'):
        fill=_v90r65_actual_genesis_fill_ok(row,price,direction,target_fraction)
        if not fill.get('eligible'):
            _record_entry_outcome(row,'BLOCKED','R65_GENESIS_FILL_BLOCKED',blockers=fill.get('blockers'))
            print(json.dumps({
              'event':'V90_R65_GENESIS_FILL_BLOCKED','portfolio':name,'asset':asset,
              'direction':direction,'price':price,**fill,
            },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0
        opposite=_v90r59_recent_opposite_exit(c,name,asset,direction,row,ts)
        if opposite and opposite.get('blocked'):
            _record_entry_outcome(row,'BLOCKED',opposite.get('reason') or 'RECENT_OPPOSITE_EXIT')
            return 0.0
        # Use the pre-R59 mutation path only for this tightly bounded genesis lane;
        # re-entry, stop-risk, source and all earlier hard controls still apply.
        return _v90r59_base_open_or_add(
          c,p,name,asset,direction,price,target_fraction,nav,ts,row,'R65_CRYPTO_TREND_GENESIS')
    return _v90r65_base_open_or_add(
      c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason)

def _v90r65_crypto_trailing_activation(asset,signed_profit):
    threshold=.0035 if str(asset) in _R65_CRYPTO else 0.0
    return {'active':bool(float(signed_profit)>=threshold),
            'threshold_pct':100.0*threshold,'signed_profit_pct':100.0*float(signed_profit)}

def _v90tr_apply(c,name,candidates,prices,ts):
    # R17 used to ratchet crypto stops after ANY positive tick. That turned
    # +0.26%-0.28% MFE into fee-negative STOPs. Before the net-profit lock has
    # room to arm, do not create a structural trailing stop for BTC/ETH.
    work=dict(candidates or {})
    try:
        positions=c.execute("SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)).fetchall()
        for z0 in positions or []:
            z=dict(z0); asset=str(z.get('asset') or '')
            if VTM.owns_position(z):
                work.pop(asset,None); continue
            if asset in work and asset in (prices or {}) and VTE.context_of(work[asset]).get('status')=='OK':
                z['payload']=_v90j_json(z.get('payload'))
                _v90r54_tighten_position_stop(c,name,z,work[asset],prices[asset],ts)
                work.pop(asset,None)
                continue
            if asset not in _R65_CRYPTO or asset not in work or asset not in (prices or {}):
                continue
            entry=_v90r65_num(z.get('avg_entry_price'))
            px=_v90r65_num((prices or {}).get(asset))
            d=str(z.get('direction') or '')
            if entry<=0 or px<=0 or d not in ('LONG','SHORT'):
                continue
            signed=(px/entry-1.0) if d=='LONG' else (entry/px-1.0)
            gate=_v90r65_crypto_trailing_activation(asset,signed)
            if not gate.get('active'):
                work.pop(asset,None)
    except Exception:
        pass
    return _v90r65_base_trailing_apply(c,name,work,prices,ts)

def report(pg_connect):
    d=dict(_v90r65_base_report(pg_connect) or {})
    d['trend_execution_r66']={'version':VTE.VERSION,'status':'ACTIVE','paper_only':True,
        'closed_candle_events':True,'nearest_senior_level_limits_entry':True,
        'distinct_confirmation_per_add':True,'initial_size_policy_preserved':True,
        'execution_quote_max_age_seconds':{'BTC':30,'ETH':30,'other':120},
        'research_validation':'SEE_REPLAY_REPORT','profitability_proven':False,
        'entry_economics':{'move_policy_version':VX.MOVE_POLICY_VERSION,
            'minimum_move_pct':VX.MIN_EXPECTED_MOVE_PCT,
            'minimum_move_cost_multiple':VX.MIN_MOVE_COST_MULTIPLE,
            'minimum_net_reward_risk':VX.MIN_REWARD_RISK,
            'positive_net_target_required':True}}
    d['crypto_early_capture_r65']={
      'status':'ACTIVE','started_at':V90_R65_STARTED_AT,
      'entry_path_revision':'2026-10-02-origin-and-executable-fill',
      'assets':['BTC','ETH'],
      'early_genesis':'5m/1h/4h current evidence can start risk before mature-history confirmation',
      'aggressive_initial':'50%; 75% only on strong non-marginal genesis, then add dynamically',
      'anti_chase':'all portfolios reprice new entries and adds from the preserved breakout origin; senior-only entries require a local trigger',
      'local_reversal_veto':True,'trigger_origin_preserved':True,
      'protective_exit_uses_observed_book':True,
      'historical_management_errors':'may reduce size but cannot force waiting until the local extreme',
      'current_hard_economics':'still authoritative',
      'crypto_structural_trailing_activation_pct':.35,
      'incident_reference':'BTC/ETH 2026-10-02 early signal seen, execution delayed to local high',
    }
    return _jsonable(d)

V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),39)



# VERITAS V90 SIGNAL-AUTHORITATIVE ENTRY R79
# User policy: when the system itself publishes a directional signal, paper
# execution must start risk rather than let an older local event veto it.
# This is a staged-entry policy, NOT a removal of source/quote/stop/risk safety.
V90_R79_STARTED_AT=os.getenv('VERITAS_R79_EPOCH','2026-10-05T10:35:00+00:00')
_v90r79_base_admission=_signal_first_admission
_v90r79_base_open_or_add=_open_or_add
_v90r79_base_report=report

_R79_SOFT_ECON_BLOCKERS=set(CTC.SOFT_VETOES)
_R79_HARD_COST_BLOCKERS={
    x for x in CTC.HARD_VETOES
    if x in {'EXPECTED_MOVE_BELOW_COST_BUFFER','TARGET_NOT_PROFITABLE_AFTER_COSTS'}
}


def _v90r83_fresh_execution_row(row,now=None):
    """Attach the freshest same-source executable quote without changing signal audit time."""
    x=dict(row or {})
    asset=str(x.get('asset') or '')
    clock=_v90r55_dt(now) or datetime.now(timezone.utc)
    signal_px=VTE.number(x.get('price'))
    x['_r83_signal_reference_price']=signal_px
    q=dict(x.get('_execution_quote') or {})

    def valid(candidate):
        if not candidate or not candidate.get('source_gate_pass'):
            return False
        if VX.is_proxy_price(asset,candidate) or not VPS.positive(candidate.get('price')):
            return False
        return bool(VPG.quote_gate(candidate.get('observed_at'),now=clock,
                                   execution=True,asset=asset).get('eligible'))

    if not valid(q):
        expected=VPS.identity(asset,x)
        if expected:
            pseudo={'asset':asset,'payload':{'price_source_lock':expected}}
            q=VPG.quote_for_position(pseudo,now=clock)
        else:
            try:
                with VPG._quotes_lock:
                    q=dict(VPG._quotes.get(asset) or {})
            except Exception:
                q={}

    if valid(q):
        x['_execution_quote']=dict(q)
        x['execution_observed_at']=q.get('observed_at')
        x['price']=float(q['price'])
        if q.get('best_bid') is not None:
            x['best_bid']=q.get('best_bid')
        if q.get('best_ask') is not None:
            x['best_ask']=q.get('best_ask')
        if q.get('market_open') is not None:
            x['market_open']=bool(q.get('market_open'))
    return x


def _v90r83_actual_chase_gate(row,price=None):
    """Measure extension at the executable price even for signal-authoritative events."""
    row=row or {}
    h=str(row.get('horizon') or '')
    if h not in _R56_TRIGGER_HORIZONS:
        return {'eligible':True,'reason':'R83_CHASE_NOT_APPLICABLE'}
    d=_v90r56_direction(row)
    px=_v90r51_num(price if price is not None else row.get('price'))
    reference=_v90r51_num(row.get('_r83_signal_reference_price'),_v90r51_num(row.get('price')))
    if d not in ('LONG','SHORT') or not px or px<=0 or not reference or reference<=0:
        return {'eligible':True,'reason':'R83_CHASE_UNMEASURED'}

    rv=abs(_v90r51_num(row.get('realized_vol'),0.0) or 0.0)
    trigger=_v90r56_trigger_level(row)
    if trigger:
        consumed=((px/trigger)-1.0) if d=='LONG' else ((trigger/px)-1.0)
        source='TRIGGER_LEVEL'
    else:
        hr=_v90r51_num(row.get('horizon_return'))
        if hr is None or hr<=-1:
            return {'eligible':True,'reason':'R83_CHASE_UNMEASURED'}
        hr=(1.0+hr)*px/reference-1.0
        consumed=max(0.0,hr if d=='LONG' else -hr)
        source='HORIZON_RETURN_REPRICED'

    ratio=0.80 if h in ('5m','1h') else 1.00
    floor={'5m':0.0040,'1h':0.0060,'4h':0.0100}.get(h,0.0060)
    limit=max(floor,ratio*max(rv,0.0025))
    late=bool(consumed>limit)
    return {
      'eligible':not late,
      'reason':'R83_WAIT_RETEST_LATE_EXECUTION' if late else 'R83_EXECUTION_TIMING_OK',
      'consumed_move_pct':consumed,'late_entry_limit_pct':limit,
      'realized_vol':rv,'measurement_source':source,
      'trigger_level':trigger,'evaluated_price':px,'signal_reference_price':reference,
    }


# R79 strategy mutation chain retired from production by CTC v2.
# R83 fresh-quote / anti-chase helpers above remain audit-compatible.
V90_CORE_LEARNING_LAYERS=max(int(V90_CORE_LEARNING_LAYERS),40)


# R80: the position owns its quote source for its entire lifetime.
_r80_base_step_one=_step_one
_r80_base_step_all=step_all
_r80_base_close_or_reduce=_close_or_reduce
_r80_incident_checked=False


def _r80_quarantine_source_incident(pg_connect):
    global _r80_incident_checked
    if _r80_incident_checked:
        return
    # Reconciled against the 2026-10-05 journal + Render market_verified logs.
    # Preserve the accounting ledger. Exclude contaminated marks from learning.
    ids=['Impulse:GOLD:1791207888095','Aggressive:GOLD:1791207888305',
         'Champion:GOLD:1791207888491','Challenger:GOLD:1791207888980']
    audit={'data_integrity_status':'MIXED_PRICE_SOURCES','learning_eligible':False,
           'learning_exclusion_reason':'ENTRY_PROFINANCE_MARK_GLD_GC_PROXY',
           'source_integrity_incident':'GOLD_20261005_1344_1400',
           'source_integrity_note':'ProFinance entry; GLD/GC proxy marks observed in 13:57-14:00 UTC logs. Ledger retained.'}
    with pg_connect() as c, c.transaction():
        c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                  "WHERE trade_id=ANY(%s) AND payload->>'source_integrity_incident' IS DISTINCT FROM %s",
                  (json.dumps(audit),ids,audit['source_integrity_incident']))
        c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                  "WHERE active_trade_id=ANY(%s) AND payload->>'source_integrity_incident' IS DISTINCT FROM %s",
                  (json.dumps(audit),ids,audit['source_integrity_incident']))
    _v90r44_sanitize_state['at']=0.0
    _v90r29_cache['at']=0.0; _v90r33_cache['at']=0.0
    _r80_incident_checked=True


def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):
    for row in summary or []:
        VPG.publish_quote(row.get('asset'),VPS.quote_from_row(row))
    _r80_quarantine_source_incident(pg_connect)
    with pg_connect() as c:
        positions=[dict(z) for z in c.execute(VPG.QUOTE_POSITION_SQL).fetchall()]
    if VPG._entry_namespace is not None:
        VPG.refresh_position_quotes(VPG._entry_namespace,positions)
    positions = None
    return _r80_base_step_all(summary,pg_connect,model_version,observed_at,VC.COMMISSION_RATE,emit)


def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):
    import veritas_thesis_guard as VTG
    rows=[dict(z) for z in c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s',(name,)).fetchall()]
    safe_prices=dict(prices or {}); safe_candidates=dict(candidates or {}); safe_summary=list(summary or [])
    for z in rows:
        asset=z['asset']; identity=VPS.position_identity(z); q=VPG.quote_for_position(z,now=ts)
        safe_prices[asset]=float(q['price']) if q else VPS.frozen_price(z)
        status='OK' if q else 'PINNED_SOURCE_QUOTE_UNAVAILABLE'
        audit={'price_source_status':status}
        if identity:
            audit['price_source_lock']=identity
        if q:
            audit['source_locked_mark']={'identity':VPS.identity(asset,q),'price':float(q['price']),
                                         'observed_at':q['observed_at']}
        c.execute("UPDATE paper_positions SET last_price=%s,payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                  "WHERE portfolio_name=%s AND asset=%s",
                  (safe_prices[asset],json.dumps(audit),name,asset))
        if z.get('active_trade_id'):
            c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                      (json.dumps(audit),z['active_trade_id']))
        candidate=safe_candidates.get(asset)
        # Foreign-source indicator changes cannot invalidate a held position.
        def usable(row):
            return bool(q and VPS.same(identity,VPS.identity(asset,row))
                        and VPS.same(identity,VPS.identity(asset,VPS.quote_from_row(row))))
        if candidate and not usable(candidate):
            safe_candidates.pop(asset,None)
        safe_summary=[r for r in safe_summary if r.get('asset')!=asset or usable(r)]
        peer=VPI.find(c,z)
        if peer.get('active') and q:
            peer_patch={'shared_canonical_setup_invalidation':peer}
            encoded=json.dumps(peer_patch,ensure_ascii=False,default=str)
            c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE portfolio_name=%s AND asset=%s AND active_trade_id=%s",
                      (encoded,name,asset,z.get('active_trade_id')))
            if z.get('active_trade_id'):
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                          (encoded,z.get('active_trade_id')))
            p_now,pos_now=_portfolio_rows(c,name)
            nav_now,_,_,_=_mark_nav(p_now,pos_now,safe_prices)
            closed=canonical_close_or_reduce(
                c,p_now,name,z,safe_prices[asset],0.0,nav_now,ts,
                'HARD_THESIS_INVALIDATION_SHARED_CANONICAL_SETUP'
            )
            if closed:
                continue
        maturity=VPM.observe(c,name,z,q,ts,commission=VC.COMMISSION_RATE) if q else {}
        if maturity.get('position'):
            z=maturity['position']
        if VTM.owns_position(z):
            VTM.apply_trailing(c,name,z,safe_summary,q,ts)
            safe_candidates,safe_summary=VTM.filter_lower_context(z,safe_candidates,safe_summary)
        safe_candidates,safe_summary,guard=VTG.guard_open_position(c,z,safe_candidates,safe_summary,now=ts)
        if guard.get('active'):
            patch={'ctc_senior_thesis_guard':guard}
            c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE portfolio_name=%s AND asset=%s",
                      (json.dumps(patch),name,asset))
            if z.get('active_trade_id'):
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb WHERE trade_id=%s",
                          (json.dumps(patch),z['active_trade_id']))
    rows = z = None
    return _r80_base_step_one(c,name,policy,safe_candidates,safe_prices,ruonia,usdrub,ts,VC.COMMISSION_RATE,safe_summary)


def _close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    q=VPG.exit_execution_quote(dict(z),now=ts)
    if not q:
        return 0.0
    actual=float(q['price'])
    if str(reason)=='STOP' and VPG.protective_reason(dict(z),q,VPG.utc_datetime(ts))!='STOP':
        return 0.0
    return _r80_base_close_or_reduce(c,p,name,dict(z,_execution_quote=q,_execution_quote_frozen=True),actual,target_fraction,nav,ts,reason)


# CANONICAL FINAL RUNTIME AUTHORITY — CTC v2
# Historical Rxx functions remain replay/lifecycle compatibility only. Candidate
# routing, admission, sizing and mutation authority are replaced below.
def _canonical_desired_fraction(row,policy,drawdown):
    out=VCR.evaluate(row,policy,drawdown)
    if isinstance(row,dict): VAT.record(row,out,out.get('checked_at'),'ALLOCATION')
    return float(out.get('fraction') or 0.0) if out.get('open') else 0.0


def _canonical_payload(z):
    p=(z or {}).get('payload') or {}
    if isinstance(p,dict):
        return dict(p)
    try:
        return json.loads(p)
    except Exception:
        return {}


def canonical_open_or_add(c,p,name,asset,direction,price,target_fraction,nav,ts,row,reason):
    row={} if row is None else row
    row.setdefault('_execution_audit',{})
    row.setdefault('_admission_audit',{}).pop('FILL',None)
    row=dict(row or {})
    if row.get('_runtime_quote_refresh'):
        ts=datetime.now(timezone.utc).isoformat()
        row=VPG.refresh_execution_row(row,now=VPG.utc_datetime(ts))
        price=VPS.positive(VPS.quote_from_row(row).get('price')) or price
        # Freeze this selected quote/time through admission and accounting.
        row['_runtime_quote_refresh']=False
    quote=VPS.quote_from_row(row)
    price=VPS.positive(quote.get('price')) or price
    row=VPS.execution_row(dict(row,_execution_quote=quote))
    policy=dict(POLICIES.get(str(name)) or {})
    hwm=float((p or {}).get('high_water_nav_rub') or nav or 1.0)
    dd=max(0.0,1.0-float(nav)/max(hwm,1.0))
    row=TFP.prepare_row(dict(row or {},research_decision=direction),price,ts)
    admission=VCR.evaluate(row,policy,dd,ts)
    VAT.record(row,admission,ts,'EXECUTION')
    row['_execution_audit']['checked_at']=str(ts)
    if not admission.get('open'):
        _record_entry_outcome(row,'BLOCKED',admission.get('reason') or 'CANONICAL_ADMISSION_BLOCK',
                              canonical_admission=admission)
        return 0.0
    existing=c.execute(
        "SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s",
        (name,asset)).fetchone()
    requested=max(0.0,float(target_fraction or 0.0))
    event=(admission.get('trend_event') or VTE.context_of(row or {}).get('event') or {})
    event_id=event.get('event_id')
    if event_id and c.execute("""SELECT 1 AS ok FROM paper_orders
        WHERE portfolio_name=%s AND asset=%s AND side=%s
          AND COALESCE(payload->>'entry_event_id',
                       payload#>>'{user_teaching_trace,timeframe_entry_context,event,event_id}',
                       payload#>>'{entry_timing,event_id}')=%s LIMIT 1""",
        (name,asset,'BUY' if direction=='LONG' else 'SELL_SHORT',str(event_id))).fetchone():
        _record_entry_outcome(row,'BLOCKED','EVENT_REUSE_WITHOUT_NEW_CONFIRMATION',event_id=event_id)
        return 0.0
    if not existing:
        requested=min(requested or float(admission['fraction']),float(admission['fraction']))
        if event_id:
            prior=c.execute("""SELECT 1 AS ok FROM paper_trades
                WHERE portfolio_name=%s AND asset=%s AND direction=%s
                  AND payload->>'r66_event_id'=%s
                  AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
                LIMIT 1""",(name,asset,direction,str(event_id))).fetchone()
            if prior:
                _record_entry_outcome(row,'BLOCKED','EVENT_REUSE_WITHOUT_NEW_CONFIRMATION',
                                      canonical_admission=admission,event_id=event_id)
                return 0.0
    elif str(existing.get('direction') or '')==str(direction):
        import veritas_structural_lifecycle as VSL
        pending = (VPG.protective_reason(existing,quote,VPG.utc_datetime(ts))
                   if VSL.owns_position(existing) else None)
        if pending:
            _record_entry_outcome(row,'HELD','STRUCTURAL_PROTECTIVE_EXIT_PENDING',
                                  pending_protective_reason=pending)
            return 0.0
        current=abs(float(existing.get('units') or 0.0)*float(price))/max(float(nav),1.0)
        if requested<=current+0.0025:
            _record_entry_outcome(row,'HELD','TARGET_ALREADY_REACHED',current_fraction=current)
            return 0.0
        avg=float(existing.get('avg_entry_price') or price)
        favorable=((float(price)/avg)-1.0)*(1 if direction=='LONG' else -1)
        if favorable<=0:
            _record_entry_outcome(row,'BLOCKED','CANONICAL_NO_AVERAGING_LOSER',
                                  current_fraction=current,favorable_progress=favorable)
            return 0.0
        payload=_canonical_payload(existing)
        import veritas_structural_lifecycle as VSL
        structural_add=TFP.structural_quote_rule(row)
        binding=VSL.add_binding(existing,row) if structural_add else None
        if structural_add and not binding.get('eligible'):
            _record_entry_outcome(row,'BLOCKED',binding['reason'])
            return 0.0
        if not structural_add and str(payload.get('execution_horizon') or '')!=str(row.get('horizon') or ''):
            _record_entry_outcome(row,'BLOCKED','SAME_TF_ADD_HORIZON_MISMATCH')
            return 0.0
        last_trace=(payload.get('last_add_teaching_trace') or {}).get('timeframe_entry_context') or {}
        used={str(x) for x in (payload.get('r66_event_id'),payload.get('last_add_event_id'),
                               (last_trace.get('event') or {}).get('event_id')) if x}
        new_event=event_id
        if new_event and str(new_event) in used:
            _record_entry_outcome(row,'BLOCKED','CANONICAL_ADD_REQUIRES_NEW_CONFIRMATION',
                                  event_id=new_event)
            return 0.0
        actual=VX.entry_gate(row,float(price),direction,requested,existing,
                            existing_target_price=VX.stored_position_target_price(existing),now=VPG.utc_datetime(ts),
                            execution_fraction=max(0.0,requested-current))
        hard=[x for x in (actual.get('blockers') or []) if CTC.veto_severity(x)=='HARD']
        if hard:
            _record_entry_outcome(row,'BLOCKED',hard[0],blockers=hard,canonical_add_gate=actual)
            return 0.0
    else:
        if not bool((row or {}).get('_flip_confirmed')):
            _record_entry_outcome(row,'BLOCKED','DIRECTION_FLIP_NOT_CONFIRMED')
            return 0.0
        requested=min(requested or float(admission['fraction']),float(admission['fraction']))

    cap=float(policy.get('max_fraction') or policy.get('max_single_asset_fraction') or 0.0)
    if cap>0:
        requested=min(requested,cap)
    step=float(policy.get('position_step') or .05)

    # Once an episode has earned/realized profit, an ADD may use only the
    # remaining whole-cycle profit buffer at the active stop. Keep trend
    # pyramiding, but never let it turn the already-earned episode negative.
    if (existing and str(existing.get('direction') or '')==str(direction)
            and VEF.protection_active(existing,{})):
        tid=existing.get('active_trade_id')
        trade=(c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",
                         (tid,)).fetchone() if tid else None)
        floor=VEF.assess_add(existing,dict(trade or {}),price,requested,nav)
        if floor.get('active'):
            encoded=json.dumps({'episode_profit_floor_last_check':floor},ensure_ascii=False,default=str)
            c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE portfolio_name=%s AND asset=%s",(encoded,name,asset))
            if tid:
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                          "WHERE trade_id=%s",(encoded,tid))
            current=float(floor.get('current_fraction') or 0.0)
            allowed=max(0.0,float(floor.get('cap_fraction') or current)-current)
            stepped=current+math.floor(allowed/step+1e-9)*step
            if stepped<requested-1e-9:
                requested=max(current,stepped)
                row['_episode_profit_floor_cap']=floor
            if requested<=current+0.0025:
                _record_entry_outcome(row,'BLOCKED','EPISODE_PROFIT_FLOOR_ADD_BLOCKED',
                                      episode_profit_floor=floor,current_fraction=current)
                return 0.0

    if not existing or str(existing.get('direction'))!=str(direction):
        requested=max(0.0,math.floor(requested/step+1e-9)*step)
    if requested<=0:
        _record_entry_outcome(row,'BLOCKED','STOP_RISK_CAP_EXCEEDED')
        return 0.0
    work=dict(row or {})
    work['trade_plan']=dict(admission.get('prepared_plan') or work.get('trade_plan') or {})
    work['_canonical_admission']={k:v for k,v in admission.items() if k!='prepared_plan'}
    if admission.get('probability_source')=='PROSPECTIVELY_VALIDATED_CALIBRATION':
        work['_pwin']=admission['probability']
        work['_pwin_source']=admission['probability_source']
    work.setdefault('_pwin',admission.get('probability') or work.get('confidence') or .5)
    work.setdefault('_pwin_source',admission.get('probability_source') or 'CTC_V2_CANONICAL')
    # The accounting function intentionally returns None after a successful
    # entry. Its shared audit is the confirmation, never the fee return value.
    # Reset it so an earlier execution on a reused candidate cannot certify one.
    if not isinstance(work.get('_execution_audit'),dict):
        work['_execution_audit']={}
    work['_execution_audit'].update(status='NOT_EXECUTED',reason='ACCOUNTING_PENDING')
    result = _vp_base.CANONICAL_ACCOUNTING_OPEN_OR_ADD(
        c,p,name,asset,direction,float(price),requested,nav,ts,work,'CTC_V2_'+str(reason or 'ENTRY'))
    if work['_execution_audit'].get('status')=='EXECUTED':
        # Observe the exact selected entry quote only after accounting confirms
        # a fill.  A carried/add position never receives a fabricated prefix.
        import veritas_observation_path as VOP
        from contextlib import nullcontext
        try:
            transaction=getattr(c,'transaction',None)
            with transaction() if callable(transaction) else nullcontext():
                opened = c.execute('SELECT * FROM paper_positions WHERE portfolio_name=%s AND asset=%s',
                                   (name,asset)).fetchone()
                if (opened and opened.get('asset')==asset and opened.get('direction')==direction
                        and opened.get('active_trade_id')):
                    is_new = not existing or opened.get('active_trade_id') != existing.get('active_trade_id')
                    VOP.record(c,dict(opened),VPS.quote_from_row(work),ts,
                               at_entry=is_new,lane='CANONICAL_ENTRY' if is_new else 'CANONICAL_ADD')
        except Exception:
            # A missing entry witness stays unverified; an optional evidence
            # read/write must not roll back the already accounted paper fill.
            pass
    return result


def canonical_close_or_reduce(c,p,name,z,price,target_fraction,nav,ts,reason):
    z=dict(z or {})
    q=VPG.exit_execution_quote(z,now=ts)
    if not q:
        return 0.0
    z.update(_execution_quote=q,_execution_quote_frozen=True)
    actual=float(q['price'])
    reason=str(reason or '')
    current=abs(float(z.get('units') or 0.0)*actual)/max(float(nav),1.0)
    full=float(target_fraction or 0.0)<=0.0
    full_ok=reason.startswith((
        'STOP','TAKE_PROFIT','HARD_THESIS','V84_CONFIRMED_DIRECTION_FLIP',
        'V842_CONFIRMED_DIRECTION_FLIP',
        'STRUCTURE_BREAK','STRUCTURE_EXHAUSTION','INSTRUMENT_REPLACED',
        'PORTFOLIO_HARD_STOP','RISK_HARD_STOP','PRODUCTION_CANDIDATE_REBASE',
        'LEGACY_KERNEL_REBASE','SOURCE_INCIDENT_QUARANTINE'))
    partial_ok=(not full and (
        reason.startswith(('TAKE_PROFIT','DYNAMIC_PARTIAL_PROFIT','PROFIT_HARVEST',
                           'EDGE_DECAY','RISK_REDUCTION','TRAILING_REDUCTION'))
        or float(target_fraction)<current))
    if not (full_ok or partial_ok):
        print(json.dumps({'event':'CTC_V2_EXIT_BLOCKED','portfolio':name,'asset':z.get('asset'),
                          'reason':reason,'target_fraction':target_fraction},
                         ensure_ascii=False,separators=(',',':')),flush=True)
        return 0.0
    if reason=='STOP' and VPG.protective_reason(z,q,VPG.utc_datetime(ts))!='STOP':
        return 0.0

    # CTC lifecycle: the first take-profit harvests part of a position and keeps
    # a structural runner whenever the 5% position step permits it. Never take
    # discretionary profit unless the whole-trade result is positive after costs.
    if VPG.is_discretionary_profit_exit(reason):
        trade=(c.execute("SELECT * FROM paper_trades WHERE trade_id=%s",
                         (z.get('active_trade_id'),)).fetchone()
               if z.get('active_trade_id') else None)
        assessment=VPG.profit_exit_assessment(
            z,q,dict(trade or {}),nav,getattr(_vp_base,'COMMISSION',VC.COMMISSION_RATE))
        if not assessment.get('eligible'):
            print(json.dumps({'event':'CTC_V2_TP_SUPPRESSED','portfolio':name,
                              'asset':z.get('asset'),'reason':assessment.get('reason'),
                              'projected_net_pnl_rub':assessment.get('net_pnl_rub'),
                              'exit_reason':reason,'target_fraction':target_fraction},
                             ensure_ascii=False,default=str,separators=(',',':')),flush=True)
            return 0.0
    # Every actual close/reduction records the same quote used by accounting.
    # Evidence-only metadata does not alter stop, target, size or execution price.
    import veritas_observation_path as VOP
    z = VOP.record(c,z,q,ts,lane='CANONICAL_EXIT')
    import veritas_structural_lifecycle as VSL
    if full and reason.startswith('TAKE_PROFIT') and VSL.owns_position(z):
        reduction=VSL.target_reduction(z,actual,nav,ts)
        if not reduction.get('eligible'):
            return 0.0
        result=_vp_base.CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE(
            c,p,name,dict(z,_execution_quote=q),actual,reduction['target_fraction'],nav,ts,reduction['reason'])
        if result:
            patch=dict(reduction['patch'],profit_exit_assessment=assessment)
            encoded=json.dumps(patch,ensure_ascii=False,default=str)
            c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE active_trade_id=%s",(encoded,z.get('active_trade_id')))
            c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                      "WHERE trade_id=%s",(encoded,z.get('active_trade_id')))
        return result
    if full and reason.startswith('TAKE_PROFIT'):
        policy=dict(POLICIES.get(str(name)) or {})
        step=float(policy.get('position_step') or CTC.LIFECYCLE_POLICY['minimum_position_step'])
        payload=_canonical_payload(z)
        peak=max(current,float(payload.get('peak_fraction') or 0.0),
                 float((trade or {}).get('max_fraction') or 0.0))
        from veritas_strategy_roles import runner_ratio
        ratio=runner_ratio(str(name),payload)
        desired=max(step,math.floor((peak*ratio)/step+1e-9)*step)
        # A 10%+ position must realize at least one 5% step and keep a runner.
        if peak>=2.0*step-1e-9 and current>step+0.0025:
            # Position notional can drift slightly below its nominal fraction as
            # price moves. Eligibility for a runner is based on the peak/entered
            # size, while the reduction uses the current notional.
            desired=min(desired,max(step,current-step))
            desired=max(step,desired)
            result=_vp_base.CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE(
                c,p,name,dict(z,_execution_quote=q),actual,desired,nav,ts,
                'TAKE_PROFIT_PARTIAL_CTC_V2')
            if result:
                patch={'r17_tp1_done':True,'r17_tp1_at':str(ts),
                       'r17_tp1_price':actual,'runner_floor_fraction':desired,
                       'profit_exit_policy':'CTC_V2_PARTIAL_TP_THEN_STRUCTURAL_RUNNER',
                       'profit_exit_assessment':assessment}
                c.execute("UPDATE paper_positions SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                          "WHERE active_trade_id=%s",
                          (json.dumps(patch,ensure_ascii=False,default=str),z.get('active_trade_id')))
                c.execute("UPDATE paper_trades SET payload=COALESCE(payload,'{}'::jsonb)||%s::jsonb "
                          "WHERE trade_id=%s",
                          (json.dumps(patch,ensure_ascii=False,default=str),z.get('active_trade_id')))
            return result

    return _vp_base.CANONICAL_ACCOUNTING_CLOSE_OR_REDUCE(
        c,p,name,dict(z,_execution_quote=q),actual,target_fraction,nav,ts,reason)


class CanonicalAdmissionEngine:
    version=CTC.VERSION
    def evaluate(self,row,policy,drawdown):
        out=dict(VCR.evaluate(row,dict(policy or {}),drawdown) or {})
        out['canonical_policy_version']=self.version
        out['canonical_runtime_version']=VCR.VERSION
        out['canonical_stage_order']=list(CTC.STAGE_ORDER)
        out['objective_hard_constraint']=CTC.OBJECTIVE_POLICY['hard_constraint']
        out['objective_priority']=list(CTC.OBJECTIVE_POLICY['priority_order'])
        source_row=row or {}
        out.setdefault('paper_source_quality',
                       'PRODUCTION_GRADE' if source_row.get('production_eligible') else 'RESEARCH_GRADE')
        out.setdefault('paper_is_live_fill_evidence',False)
        return out


CANONICAL_ADMISSION_ENGINE=CanonicalAdmissionEngine()


def canonical_signal_first_admission(row,policy,drawdown):
    out=CANONICAL_ADMISSION_ENGINE.evaluate(row,policy,drawdown)
    if isinstance(row,dict): VAT.record(row,out,out.get('checked_at'),'PROJECTION')
    return out


def canonical_report(pg_connect):
    d=dict(report(pg_connect) or {})
    d=VCP.decorate_report(d)
    d['runtime_authority']={
        'version':CTC.BASIS_RUNTIME,'policy_version':CTC.VERSION,
        'legacy_admission_authoritative':False,
        'legacy_candidate_routing_authoritative':False,
    }
    d['release']=VR.snapshot()
    return _jsonable(d)


# Patch strategy-routing globals used by historical lifecycle wrappers. This is
# what prevents those wrappers from silently deleting/re-ranking current signals.
_candidate_book_v84=VCR.candidate_book
_best_impulse_by_asset=VCR.impulse_candidate_book
_v90_aggressive_candidate_book=VCR.aggressive_candidate_book
_currency_candidate_book=VCR.currency_candidate_book
_v90_trend_transition_candidate_book=VCR.transition_candidate_book
_desired_fraction=_canonical_desired_fraction

FINAL_RUNTIME_AUTHORITY_VERSION=CTC.BASIS_RUNTIME
FINAL_SIGNAL_FIRST_ADMISSION=canonical_signal_first_admission
FINAL_OPEN_OR_ADD=canonical_open_or_add
FINAL_CLOSE_OR_REDUCE=canonical_close_or_reduce
FINAL_STEP_ONE=_step_one
FINAL_STEP_ALL=step_all
FINAL_REPORT=canonical_report

# Import-order-independent binding. Legacy helpers stay inspectable but cannot
# replace canonical candidate/admission/size/mutation authority.
_vp_base._candidate_book_v84=VCR.candidate_book
_vp_base._best_impulse_by_asset=VCR.impulse_candidate_book
_vp_base._v90_aggressive_candidate_book=VCR.aggressive_candidate_book
_vp_base._currency_candidate_book=VCR.currency_candidate_book
_vp_base._v90_trend_transition_candidate_book=VCR.transition_candidate_book
_vp_base._desired_fraction=_canonical_desired_fraction
_vp_base._signal_first_admission=FINAL_SIGNAL_FIRST_ADMISSION
_vp_base._open_or_add=FINAL_OPEN_OR_ADD
_vp_base._close_or_reduce=FINAL_CLOSE_OR_REDUCE
_vp_base._step_one=FINAL_STEP_ONE
_vp_base.step_all=FINAL_STEP_ALL
_vp_base.report=FINAL_REPORT
_vp_base._VERITAS_RUNTIME=__import__(__name__)

_PRODUCTION_AUTHORITY_NAMES={
    '_candidate_book_v84','_best_impulse_by_asset','_v90_aggressive_candidate_book',
    '_currency_candidate_book','_v90_trend_transition_candidate_book','_desired_fraction',
    '_signal_first_admission','_open_or_add','_close_or_reduce','_step_one','step_all','report'
}
for _compat_name,_compat_value in list(globals().items()):
    if (_compat_name.startswith(('_v90','_r'))
            and _compat_name not in _PRODUCTION_AUTHORITY_NAMES):
        setattr(_vp_base,_compat_name,_compat_value)

def runtime_authority_snapshot():
    return {
      'version':FINAL_RUNTIME_AUTHORITY_VERSION,'policy_version':CTC.VERSION,
      'canonical_runtime':VCR.VERSION,'release':VR.snapshot(),
      'candidate_book':'veritas_canonical_runtime.candidate_book',
      'signal_first_admission':FINAL_SIGNAL_FIRST_ADMISSION.__name__,
      'open_or_add':FINAL_OPEN_OR_ADD.__name__,
      'close_or_reduce':FINAL_CLOSE_OR_REDUCE.__name__,
      'step_one':FINAL_STEP_ONE.__name__,'step_all':FINAL_STEP_ALL.__name__,
      'report':FINAL_REPORT.__name__,
      'legacy_admission_authoritative':False,
      'legacy_candidate_routing_authoritative':False,
      'lifecycle_compatibility':'RXX_MANAGEMENT_ONLY',
    }


# Export only names added or replaced by canonical runtime layers.
__all__ = [
    k for k, v in globals().items()
    if not k.startswith('__')
    and k not in {'_vp_base', '_BASE'}
    and (k not in _BASE or v is not _BASE[k])
]
