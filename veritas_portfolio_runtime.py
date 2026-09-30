"""Canonical VERITAS portfolio runtime layers R42-R46.

Separated from veritas_portfolio.py to keep the base engine stable and prevent
further monkey-patch accumulation in the core module. The runtime module is
loaded only after the R41 base has finished initializing.
"""
import veritas_portfolio as _vp_base

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
    out = []
    for asset, row in sorted((candidates or {}).items()):
        sf = _signal_first_admission(row, policy, drawdown)
        plan = row.get('trade_plan') or {}
        out.append({
            'asset':asset,
            'direction':row.get('research_decision'),
            'horizon':row.get('horizon'),
            'canonical_setup_id':_portfolio_canonical_setup_id(row),
            'pwin':sf.get('probability'),
            'model_quality_score':sf.get('model_quality_score'),
            'signal_prior':row.get('_pwin'),
            'probability_source':sf.get('probability_source') or row.get('_pwin_source'),
            'rank':row.get('_rank'),
            'rr':plan.get('expected_to_stop_ratio'),
            'hard_veto':not bool(sf.get('open')),
            'target_fraction':sf.get('fraction'),
            'reason':sf.get('reason'),
            'economics_blockers':sf.get('economics_blockers'),
            'profitability_blockers':sf.get('profitability_blockers'),
            'profitability_gate':sf.get('profitability_gate'),
            'source_blockers':sf.get('source_blockers'),
            'quote_time_gate':sf.get('quote_time_gate'),
            'modeled_round_trip_cost_pct':sf.get('modeled_round_trip_cost_pct'),
            'net_reward_risk':sf.get('net_reward_risk'),
            'supporting_horizons':row.get('_supporting_horizons'),
            'direction_support':row.get('_direction_support'),
            'flip_confirmed':row.get('_flip_confirmed'),
            'experience_decision':sf.get('experience_decision') or plan.get('execution_policy'),
        })
    return out

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
          'win_rate':m.get('win_rate') is not None and float(m['win_rate'])>=thresholds['min_win_rate'],
          'profit_factor':m.get('profit_factor') is not None and float(m['profit_factor'])>=thresholds['min_profit_factor'],
          'positive_net_pnl':float(m.get('net_pnl_rub') or 0.0)>0,
          'positive_avg_trade':float(m.get('avg_net_pnl_rub') or 0.0)>0,
          'drawdown':float(m.get('max_drawdown') or 0.0)<=thresholds['max_drawdown'],
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
    global_haircut=1.0
    if episodes>=10:
        try:
            global_haircut=_clip(float(_v90r33_cache.get('edge_haircut') or 1.0),0.60,1.0)
        except Exception:
            global_haircut=1.0

    profile,key=_v90r29_profile_for_row(row)
    profile_haircut=1.0
    if profile and int(profile.get('n') or 0)>=8:
        realized=_v90r29_num(profile.get('avg_movement_realization_ratio'))
        if realized is not None:
            profile_haircut=_clip(0.55+0.45*max(0.0,min(1.0,float(realized))),0.60,0.95)

    # Use the more conservative clean-sample estimate. This is the same bounded
    # calibration principle as R33, now connected to the final R42 authority.
    haircut=min(global_haircut,profile_haircut)

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
      'active':bool(episodes>=10 or (profile and int(profile.get('n') or 0)>=8)),
      'episodes':episodes,
      'global_edge_haircut':global_haircut,
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

    if learn.get('active'):
        calibrated_expected=float(learn.get('calibrated_expected_move_pct') or 0.0)
        calibrated_rr=float(learn.get('calibrated_net_reward_risk') or 0.0)
        calibrated_cost=float(learn.get('calibrated_cost_to_edge_ratio') or 999.0)

        # Friction is compared with empirically realizable movement, not the
        # original forecast. This directly addresses COST_DRAG + EDGE_OVERFORECAST.
        max_cost_ratio=0.30 if mode in ('IMPULSE_ONLY','AGGRESSIVE') else 0.25
        if calibrated_expected<=0 or calibrated_cost>max_cost_ratio:
            blockers.append('LEARNED_COST_TO_REALIZED_EDGE_TOO_HIGH')

        # Closed-loop economics are authoritative for every portfolio.
        # Impulse/Aggressive may accept lower thresholds than the production
        # candidates, but they may no longer open negative/near-unit learned R/R
        # merely because they are "research" books.
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
            # Range regimes need a confirmed break/retest, not an early probe.
            if regime.startswith('RANGE_'):
                blockers.append('EARLY_BREAKOUT_IN_RANGE_REGIME')

        # Context learning is directional only when R29 observed repeated
        # entry failures. Stop/capture errors deliberately do not veto entry.
        profile=learn.get('profile') or {}
        pn=int(profile.get('n') or 0)
        if pn>=8:
            entry_error=float(profile.get('entry_error_rate') or 0.0)
            cost_drag=float(profile.get('cost_drag_rate') or 0.0)
            bayes=float(profile.get('bayesian_win_rate') or 0.5)
            avg_pnl=float(profile.get('avg_net_pnl_rub') or 0.0)
            if entry_error>=0.35:
                blockers.append('LEARNED_ENTRY_DIRECTION_ERROR_CLUSTER')
            if cost_drag>=0.45:
                blockers.append('LEARNED_COST_DRAG_CLUSTER')
            if bayes<0.45 and avg_pnl<=0:
                blockers.append('LEARNED_NEGATIVE_CONTEXT_EXPECTANCY')

        # Downstream canonical sizing must consume learned economics.
        base['raw_expected_move_pct']=learn.get('raw_expected_move_pct')
        base['raw_net_reward_risk']=learn.get('raw_net_reward_risk')
        base['expected_move_pct']=learn.get('calibrated_expected_move_pct')
        base['net_reward_risk']=learn.get('calibrated_net_reward_risk')
        base['cost_to_edge_ratio']=learn.get('calibrated_cost_to_edge_ratio')

    base['blockers']=list(dict.fromkeys(blockers))
    base['eligible']=not base['blockers']
    base['status']='PASS' if base['eligible'] else 'BLOCK'
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
    now=time.time()
    if (not force
            and now-float(_v90r44_sanitize_state.get('at') or 0.0)<50.0):
        return dict(_v90r44_sanitize_state)

    changed=0
    total=0
    err=None
    try:
        with pg_connect() as c:
            _v90r29_ensure(c)
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
            changed=max(0,int(getattr(cur,'rowcount',0) or 0))
            row=c.execute("""
              SELECT COUNT(*) AS n
              FROM v90_learning_episodes
              WHERE primary_attribution='ADMINISTRATIVE_EXIT_EXCLUDED'
                 OR LOWER(COALESCE(payload->>'administrative_exit','false'))='true'
            """).fetchone()
            total=int((row or {}).get('n') or 0)
    except Exception as ex:
        err=f'{type(ex).__name__}: {ex}'[:240]

    _v90r44_sanitize_state.update({
      'at':now,
      'last_changed':changed,
      'total_admin_excluded':total,
      'last_error':err,
    })

    if changed:
        # Both learning caches must be rebuilt from the sanitized episode set.
        _v90r29_cache['at']=0.0
        _v90r33_cache['at']=0.0
        print(json.dumps({
          'event':'V90_R44_LEARNING_SANITIZED',
          'changed':changed,
          'total_admin_excluded':total,
          'policy':'ADMINISTRATIVE_REBASE_EXCLUDED_FROM_LEARNING',
        },ensure_ascii=False,default=str,separators=(',',':')),flush=True)
    elif err:
        print(json.dumps({
          'event':'V90_R44_SANITIZE_ERROR',
          'error':err,
        },ensure_ascii=False,separators=(',',':')),flush=True)
    return dict(_v90r44_sanitize_state)

def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):
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
        r=c.execute("""
          SELECT COUNT(*) AS closed_trades,
                 COUNT(DISTINCT COALESCE(payload->>'canonical_setup_id',trade_id)) AS unique_episodes,
                 COUNT(*) FILTER(WHERE net_pnl_rub>0) AS wins,
                 COALESCE(SUM(net_pnl_rub),0) AS net_pnl_rub,
                 COALESCE(AVG(net_pnl_rub),0) AS avg_net_pnl_rub,
                 COALESCE(SUM(CASE WHEN net_pnl_rub>0 THEN net_pnl_rub ELSE 0 END),0) AS gross_wins_rub,
                 ABS(COALESCE(SUM(CASE WHEN net_pnl_rub<0 THEN net_pnl_rub ELSE 0 END),0)) AS gross_losses_rub,
                 COALESCE(SUM(fees_rub+funding_rub),0) AS costs_rub,
                 COUNT(*) FILTER(
                   WHERE COALESCE(payload->>'exit_reason',payload->>'close_reason','') IN ('','UNKNOWN')
                 ) AS unknown_exits
          FROM paper_trades
          WHERE portfolio_name=%s
            AND opened_at >= %s::timestamptz
            AND COALESCE(payload->>'execution_cohort','')=%s
            AND UPPER(COALESCE(payload->>'exit_reason',payload->>'close_reason','')) NOT LIKE '%%REBASE%%'
            AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
        """,(name,V90_R45_PRODUCTION_EVIDENCE_EPOCH,V90_R45_EXECUTION_COHORT)).fetchone()
        dd=c.execute("""
          WITH x AS (
            SELECT observed_at,nav_rub,
                   MAX(nav_rub) OVER (
                     ORDER BY observed_at
                     ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
                   ) AS hwm
            FROM paper_nav_history
            WHERE portfolio_name=%s
              AND observed_at >= %s::timestamptz
          )
          SELECT COALESCE(
                   MAX(CASE WHEN hwm>0 THEN (hwm-nav_rub)/hwm ELSE 0 END),0
                 ) AS max_drawdown
          FROM x
        """,(name,V90_R45_PRODUCTION_EVIDENCE_EPOCH)).fetchone()
        ex=c.execute("""
          SELECT COUNT(*) AS n
          FROM paper_trades
          WHERE portfolio_name=%s
            AND opened_at >= %s::timestamptz
            AND (closed_at IS NOT NULL OR status IN ('CLOSED','CLOSE','EXITED'))
            AND (
              COALESCE(payload->>'execution_cohort','')<>%s
              OR UPPER(COALESCE(payload->>'exit_reason',payload->>'close_reason','')) LIKE '%%REBASE%%'
            )
        """,(name,V90_R45_PRODUCTION_EVIDENCE_EPOCH,V90_R45_EXECUTION_COHORT)).fetchone()
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
      'evidence_epoch':V90_R45_PRODUCTION_EVIDENCE_EPOCH,
      'execution_cohort':V90_R45_EXECUTION_COHORT,
      'noncomparable_closed_trades_excluded':int((ex or {}).get('n') or 0),
      'administrative_rebase_excluded':True,
    }

def production_candidate_readiness(pg_connect):
    d=dict(_v90r45_base_readiness(pg_connect) or {})
    d['epoch']=V90_R45_PRODUCTION_EVIDENCE_EPOCH
    d['execution_cohort']=V90_R45_EXECUTION_COHORT
    d['evidence_policy']='ONLY_R45_CLEAN_CLOSED_LOOP_TRADES'
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
      'epoch':V90_R45_PRODUCTION_EVIDENCE_EPOCH,
      'execution_cohort':V90_R45_EXECUTION_COHORT,
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
    """Exit authority for the position's original execution horizon.

    INVALIDATED is authoritative once that horizon is no longer a confirmed
    trend. This prevents a stale open position from surviving indefinitely just
    because it disappeared from the fresh candidate book. A confirmed trend is
    still allowed to run, preserving the R46 trend-hold rule.
    """
    if not row:
        return False
    plan=row.get('trade_plan') or {}
    ti=plan.get('trade_integrity') or {}
    if bool(ti.get('hard_invalidation')):
        return True
    entry_quality=str(
        row.get('entry_quality')
        or plan.get('entry_quality')
        or ''
    ).upper()
    hs=row.get('horizon_structure') or {}
    hstate=str(hs.get('state') or row.get('horizon_structure_state') or '').upper()
    decision=str(row.get('research_decision') or row.get('decision') or 'NO_TRADE').upper()
    return bool(
        entry_quality=='INVALIDATED'
        and decision=='NO_TRADE'
        and hstate!='CONFIRMED_TREND'
    )

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

def _v90r46_giveback_harvest(c,p,name,prices,nav,ts):
    changes=[]
    try:
        rows=c.execute(
            "SELECT * FROM paper_positions WHERE portfolio_name=%s",(name,)
        ).fetchall()
    except Exception:
        return changes

    # Commission is 0.05% per leg. Require a positive post-cost floor rather
    # than waiting for the trade to retrace back through zero.
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

        result=_vp_base._v90r46_base_close_or_reduce(
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
        _v90r46_giveback_harvest(c,p,name,prices,nav,ts)
    except Exception:
        pass

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

# Export only names added or replaced by R42-R46.
__all__ = [
    k for k, v in globals().items()
    if not k.startswith('__')
    and k not in {'_vp_base', '_BASE'}
    and (k not in _BASE or v is not _BASE[k])
]
