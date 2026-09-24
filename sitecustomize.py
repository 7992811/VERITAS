# VERITAS v84 full upgrade bootstrap
# One-file upgrade from the stable v80.1 foundation to v81-v84.
#
# v81 - outcome learning, error attribution, rejected-signal/abstention learning
# v82 - hierarchical setup memory
# v83 - regime-adaptive sizing/confirmation/stop policy
# v84 - entry/stop/exit optimization and unified multi-timeframe portfolio routing
#
# Safety invariants:
# - source/time/kill/risk/thesis hard gates remain absolute
# - experience cannot erase a valid directional signal; minimum signal-first probe remains
# - learning is sample-gated, decayed and shrinkage-based
# - direction errors are separated from timing/stop/execution errors
# - v80.1 remains the frozen baseline for promotion tests

from pathlib import Path
from urllib.request import Request, urlopen
import sys

BASE=Path(__file__).resolve().parent
TARGET=BASE/"veritas_intelligence.py"
PORTFOLIO_TARGET=BASE/"veritas_portfolio.py"
V70_TARGET=BASE/"veritas_v70.py"

PINNED_V80_URL=(
    "https://raw.githubusercontent.com/7992811/VERITAS/"
    "8d7fd5f3e2cccf262b677707bad57d6e1e5acbc2/sitecustomize.py"
)

V80_INTEL="veritas-max-product-v80.0-unified-execution-core"
V80_PORT="veritas-portfolio-v6-v80-unified-execution"
V84_INTEL="veritas-max-product-v84.2-audited-learning-execution"
V84_PORT="veritas-portfolio-v8.2-v84-audited-execution"

START_MARKER="    # ----- v78.1 Rule & Experience Arbitration -----"
END_MARKER="    # ----- v79.0 Portfolio Trade Integrity -----"
INSERT_MARKER="    # ----- v79.0 Trade Integrity / Win-Rate Layer -----"

V84_HELPER_BLOCK='\n# =========================\n# VERITAS v81-v84 ADAPTIVE EXPERIENCE EXECUTION\n# v81: outcome learning + error attribution + rejected signals\n# v82: hierarchical setup memory\n# v83: regime-conditioned adaptive policy\n# v84: entry / stop / exit optimization\n# =========================\n\nEXPERIENCE_MIN_SIZE_N = max(6, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_SIZE_N\',\'8\')))\nEXPERIENCE_MIN_EXEC_N = max(12, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_EXEC_N\',\'20\')))\nEXPERIENCE_MIN_WEIGHT_N = max(20, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_WEIGHT_N\',\'40\')))\nEXPERIENCE_DECAY_HALF_LIFE_DAYS = max(30.0, float(os.getenv(\'VERITAS_EXPERIENCE_DECAY_HALF_LIFE_DAYS\',\'90\')))\nEXPERIENCE_MAX_EVENTS = max(500, min(20000, int(os.getenv(\'VERITAS_EXPERIENCE_MAX_EVENTS\',\'6000\'))))\nSETUP_MEMORY_CACHE_SECONDS = max(300, int(os.getenv(\'VERITAS_SETUP_MEMORY_CACHE_SECONDS\',\'3600\')))\nV84_DIRECTION_PRIOR_CAP = min(0.03, max(0.005, float(os.getenv(\'VERITAS_V84_DIRECTION_PRIOR_CAP\',\'0.025\'))))\nV84_STOP_WIDEN_CAP = min(1.40, max(1.0, float(os.getenv(\'VERITAS_V84_STOP_WIDEN_CAP\',\'1.30\'))))\n\n\ndef _v84_json(x):\n    if isinstance(x,dict): return x\n    if not x: return {}\n    try: return json.loads(x)\n    except Exception: return {}\n\n\ndef _v84_setup_family(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    piv=row.get(\'impulse_pivot_break\') or {}\n    rev=row.get(\'tactical_reversal\') or {}\n    rng=row.get(\'range_retest_breakout\') or {}\n    bq=(row.get(\'institutional_signal\') or {}).get(\'breakout_quality\') or {}\n    if piv.get(\'active\'): return \'IMPULSE_PIVOT_BREAK\'\n    if rev.get(\'active\'): return \'TACTICAL_REVERSAL\'\n    if rng.get(\'active\'): return \'RANGE_RETEST_BREAKOUT\'\n    if str(bq.get(\'state\') or \'\') in (\'EARLY_BREAKOUT\',\'CONFIRMED_BREAKOUT\'): return \'BREAKOUT\'\n    if str(plan.get(\'regime_shift_state\') or \'\') in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\'): return \'REGIME_SHIFT\'\n    return \'TREND\'\n\n\ndef _v84_entry_state(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    eq=str(plan.get(\'entry_quality\') or row.get(\'entry_quality\') or \'\')\n    life=str(plan.get(\'structure_lifecycle\') or \'\')\n    rng=row.get(\'range_retest_breakout\') or {}\n    if rng.get(\'active\') and str(rng.get(\'state\') or \'\')==\'RETEST_ENTRY\': return \'RETEST\'\n    if \'LATE\' in eq or plan.get(\'late_entry\'): return \'LATE\'\n    if \'BREAKOUT\' in eq or plan.get(\'fresh_breakout\'): return \'BREAKOUT\'\n    if life in (\'CONFIRMATION\',\'EXTENSION\') or \'CONFIRMED\' in eq: return \'CONFIRMED\'\n    if life in (\'EARLY\',\'APPROACH\') or \'EARLY\' in eq: return \'EARLY\'\n    return \'NORMAL\'\n\n\ndef _v84_regime_bucket(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    rg=str(row.get(\'regime\') or plan.get(\'regime\') or \'UNKNOWN\').upper()\n    shift=str(plan.get(\'regime_shift_state\') or \'\').upper()\n    if shift in (\'NEW_REGIME_PROVISIONAL\',\'TRANSITION\',\'OLD_REGIME_WEAKENING\') or \'TRANSITION\' in rg:\n        return \'TRANSITION\'\n    if \'PANIC\' in rg or \'STRESS\' in rg or \'HIGH_VOL\' in rg:\n        if \'RANGE\' in rg: return \'HIGH_VOL_RANGE\'\n        return \'HIGH_VOL_TREND\'\n    if \'RANGE\' in rg or \'MEAN_REVERT\' in rg: return \'RANGE\'\n    if \'UPTREND\' in rg or \'DOWNTREND\' in rg or \'TREND\' in rg: return \'TREND\'\n    return \'ADAPTIVE\'\n\n\ndef _v84_horizon_state(row=None):\n    row=row or {}\n    hs=row.get(\'horizon_structure\') or {}\n    return str(hs.get(\'state\') or \'UNKNOWN\')\n\n\ndef _v84_event_weight(ts, base=1.0):\n    now_dt=datetime.now(timezone.utc)\n    if isinstance(ts,str):\n        try: ts=datetime.fromisoformat(ts.replace(\'Z\',\'+00:00\'))\n        except Exception: ts=None\n    if ts is not None and getattr(ts,\'tzinfo\',None) is None:\n        ts=ts.replace(tzinfo=timezone.utc)\n    age=max(0.0,(now_dt-ts).total_seconds()/86400.0) if ts is not None else 0.0\n    return float(base)*math.exp(-math.log(2.0)*age/EXPERIENCE_DECAY_HALF_LIFE_DAYS)\n\n\ndef experience_direction_prior(asset,horizon,f):\n    # Bounded analog prior. It refines marginal scores but never bypasses hard gates.\n    try:\n        row=tradeability_analog_stats(asset,horizon,f,\'LONG\')\n        n=int(row.get(\'raw_n\') or 0); en=float(row.get(\'effective_n\') or 0.0)\n        p=row.get(\'positive_trade_probability\')\n        if p is None or n<TRADEABILITY_MIN_RAW_N or en<TRADEABILITY_MIN_EFFECTIVE_N:\n            return {\'status\':\'BUILDING\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                    \'raw_n\':n,\'effective_n\':en,\'p_long\':p}\n        p=float(p)\n        adj=clip((p-0.50)*0.16,-V84_DIRECTION_PRIOR_CAP,V84_DIRECTION_PRIOR_CAP)\n        return {\'status\':\'ACTIVE\',\'decision_influence\':True,\'score_adjustment\':round(adj,5),\n                \'raw_n\':n,\'effective_n\':round(en,2),\'p_long\':round(p,4),\n                \'principle\':\'bounded analog prior; hard gates remain absolute\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                \'error\':f\'{type(ex).__name__}: {ex}\'}\n\n\ndef refresh_experience_lessons(limit=400):\n    # Persist canonical trade lessons, rejected directional signals and abstentions.\n    if not pg_enabled():\n        return {\'status\':\'POSTGRES_REQUIRED\',\'trade_lessons\':0,\'rejected_lessons\':0,\'abstention_lessons\':0}\n    limit=max(50,min(1500,int(limit)))\n    trade_lessons=rejected_lessons=abstention_lessons=0\n    errors=[]\n\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT t.trade_id,t.setup_id,t.asset,t.horizon,t.direction,t.status,\n                                     t.created_at,t.closed_at,t.total_pnl_fraction,t.high_price,t.low_price,\n                                     t.avg_entry_price,t.stop_price,t.add_count,t.reduce_count,\n                                     s.payload setup_payload,\n                                     d.entity_key,d.payload decision_payload,o.payload outcome_payload\n                              FROM shadow_trades t\n                              JOIN trade_setups s ON s.setup_id=t.setup_id\n                              LEFT JOIN LATERAL (\n                                SELECT entity_key,payload FROM ledger_events\n                                WHERE event_type=\'decision\' AND asset=t.asset AND horizon=t.horizon\n                                  AND event_ts<=t.created_at\n                                ORDER BY event_ts DESC LIMIT 1\n                              ) d ON TRUE\n                              LEFT JOIN ledger_events o\n                                ON o.entity_key=d.entity_key AND o.event_type=\'outcome\'\n                              WHERE t.status<>\'ACTIVE\' AND t.total_pnl_fraction IS NOT NULL\n                              ORDER BY t.closed_at DESC NULLS LAST LIMIT %s""",(limit,)).fetchall()\n        for rr in rows:\n            x=dict(rr)\n            sp=_v84_json(x.get(\'setup_payload\')); dp=_v84_json(x.get(\'decision_payload\')); op=_v84_json(x.get(\'outcome_payload\'))\n            pnl=float(x.get(\'total_pnl_fraction\') or 0.0)\n            fr=op.get(\'forward_return\')\n            sr=None if fr is None else (float(fr) if x[\'direction\']==\'LONG\' else -float(fr))\n            rowctx={**(dp.get(\'features\') or {}),**dp}\n            plan=dp.get(\'trade_plan\') or sp\n            family=str(sp.get(\'setup_family\') or _v84_setup_family(rowctx,plan))\n            regime=_v84_regime_bucket(rowctx,plan)\n            entry_state=_v84_entry_state(rowctx,plan)\n            hstate=_v84_horizon_state(rowctx)\n\n            if pnl>0:\n                label=\'GOOD_EXECUTION\' if sr is None or sr>=0 else \'TACTICAL_WIN_AGAINST_HORIZON\'\n            elif sr is not None and sr<0:\n                label=\'DIRECTION_ERROR\'\n            elif sr is not None and sr>0 and str(x.get(\'status\'))==\'STOP\':\n                label=\'RIGHT_DIRECTION_STOP_ERROR\'\n            elif sr is not None and sr>0 and entry_state==\'LATE\':\n                label=\'RIGHT_DIRECTION_LATE_ENTRY\'\n            elif sr is not None and sr>0 and str(x.get(\'status\') or \'\') in (\'EXIT\',\'CLOSED\'):\n                label=\'RIGHT_DIRECTION_PREMATURE_EXIT\'\n            elif sr is not None and sr>0:\n                label=\'RIGHT_DIRECTION_EXECUTION_ERROR\'\n            else:\n                label=\'NEGATIVE_EXECUTION\'\n\n            entry=float(x.get(\'avg_entry_price\') or 0.0)\n            high=x.get(\'high_price\'); low=x.get(\'low_price\')\n            mfe_trade=mae_trade=None\n            if entry>0 and high is not None and low is not None:\n                if x[\'direction\']==\'LONG\':\n                    mfe_trade=float(high)/entry-1; mae_trade=float(low)/entry-1\n                else:\n                    mfe_trade=entry/float(low)-1 if float(low)>0 else None\n                    mae_trade=entry/float(high)-1 if float(high)>0 else None\n\n            payload={\n                \'trade_id\':x[\'trade_id\'],\'setup_id\':x.get(\'setup_id\'),\n                \'setup_family\':family,\'regime_bucket\':regime,\'entry_state\':entry_state,\n                \'horizon_state\':hstate,\'direction\':x[\'direction\'],\'label\':label,\n                \'profitable\':bool(pnl>0),\'actual_pnl_fraction\':pnl,\n                \'horizon_signed_return\':sr,\'outcome_mfe\':op.get(\'mfe\'),\'outcome_mae\':op.get(\'mae\'),\n                \'trade_mfe\':mfe_trade,\'trade_mae\':mae_trade,\n                \'add_count\':int(x.get(\'add_count\') or 0),\'reduce_count\':int(x.get(\'reduce_count\') or 0),\n                \'counterfactual_no_trade_utility\':0.0,\n                \'counterfactual_horizon_utility\':sr,\n                \'counterfactual_supported\':[\'NO_TRADE\',\'HOLD_TO_HORIZON\'] if sr is not None else [\'NO_TRADE\'],\n                \'counterfactual_not_identifiable\':[\'DELAYED_ENTRY_WITH_PATH_ORDER\',\'ALTERNATE_STOP_WITH_PATH_ORDER\'],\n                \'source\':\'CANONICAL_SHADOW_TRADE\',\'learning_weight\':1.0,\n            }\n            if pg_event(\'experience_lesson\',x[\'trade_id\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'closed_at\') or now()):\n                trade_lessons+=1\n    except Exception as ex:\n        errors.append(f\'trades:{type(ex).__name__}:{ex}\')\n\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload dp,o.payload op\n                              FROM ledger_events d\n                              JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type=\'outcome\'\n                              WHERE d.event_type=\'decision\'\n                              ORDER BY d.event_ts DESC LIMIT %s""",(limit*5,)).fetchall()\n        for rr in rows:\n            x=dict(rr); dp=_v84_json(x.get(\'dp\')); op=_v84_json(x.get(\'op\'))\n            research=str(dp.get(\'research_decision\') or dp.get(\'decision\') or \'NO_TRADE\')\n            executed=str(dp.get(\'decision\') or \'NO_TRADE\')\n            fr=op.get(\'forward_return\')\n            if fr is None: continue\n            fr=float(fr)\n\n            if research in (\'LONG\',\'SHORT\') and executed not in (\'LONG\',\'SHORT\'):\n                elig=dp.get(\'execution_eligibility\') or {}; plan=dp.get(\'trade_plan\') or {}\n                hard_veto=bool(\n                    (elig and not bool(elig.get(\'eligible\',True))) or\n                    ((plan.get(\'trade_integrity\') or {}).get(\'hard_invalidation\')) or\n                    (((plan.get(\'rule_arbitration\') or {}).get(\'hard_veto\') or {}).get(\'decision\')==\'VETO\')\n                )\n                sr=fr if research==\'LONG\' else -fr\n                significant=abs(fr)>=_no_trade_miss_threshold(x.get(\'horizon\'))\n                label=\'REJECTED_WINNER\' if sr>0 and significant else (\n                      \'REJECTED_LOSER\' if sr<0 and significant else \'REJECTED_NEUTRAL\')\n                rowctx={**(dp.get(\'features\') or {}),**dp}\n                payload={\n                    \'direction\':research,\'label\':label,\'signed_return\':sr,\n                    \'setup_family\':_v84_setup_family(rowctx,plan),\n                    \'regime_bucket\':_v84_regime_bucket(rowctx,plan),\n                    \'entry_state\':_v84_entry_state(rowctx,plan),\n                    \'horizon_state\':_v84_horizon_state(rowctx),\n                    \'hard_gate\':hard_veto,\n                    \'learning_weight\':0.0 if hard_veto else 1.0,\n                    \'execution_eligibility\':elig,\'reason\':plan.get(\'reason\'),\n                    \'source\':\'REJECTED_DIRECTIONAL_SIGNAL\'\n                }\n                if pg_event(\'rejected_signal_lesson\',x[\'entity_key\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'event_ts\') or now()):\n                    rejected_lessons+=1\n\n            if research==\'NO_TRADE\':\n                th=_no_trade_miss_threshold(x.get(\'horizon\'))\n                label=\'GOOD_ABSTENTION\' if abs(fr)<th else \'MISSED_LARGE_MOVE\'\n                payload={\'label\':label,\'forward_return\':fr,\'threshold\':th,\n                         \'learning_weight\':0.5,\'source\':\'ABSTENTION_OUTCOME\'}\n                if pg_event(\'abstention_lesson\',x[\'entity_key\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'event_ts\') or now()):\n                    abstention_lessons+=1\n    except Exception as ex:\n        errors.append(f\'decisions:{type(ex).__name__}:{ex}\')\n\n    if (trade_lessons+rejected_lessons+abstention_lessons)>0:\n        try:\n            setup_memory_board._cache=None\n        except Exception:\n            pass\n    return {\'status\':\'OK\' if not errors else \'DEGRADED\',\n            \'trade_lessons\':trade_lessons,\'rejected_lessons\':rejected_lessons,\n            \'abstention_lessons\':abstention_lessons,\'errors\':errors}\n\n\ndef setup_memory_board(force=False):\n    # Hierarchical setup memory. Exact states get most weight, broad families provide shrinkage.\n    cache=getattr(setup_memory_board,\'_cache\',None)\n    if cache and not force and time.time()-cache[0] < SETUP_MEMORY_CACHE_SECONDS:\n        return cache[1]\n    if not pg_enabled():\n        return {\'status\':\'POSTGRES_REQUIRED\',\'items\':[]}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT event_type,event_ts,asset,horizon,payload\n                              FROM ledger_events\n                              WHERE event_type IN (\'experience_lesson\',\'rejected_signal_lesson\')\n                              ORDER BY event_ts DESC LIMIT %s""",(EXPERIENCE_MAX_EVENTS,)).fetchall()\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'items\':[],\'error\':f\'{type(ex).__name__}: {ex}\'}\n\n    buckets={}\n    def touch(key):\n        return buckets.setdefault(key,{\'n\':0,\'w\':0.0,\'wins\':0.0,\'pnl\':0.0,\'stop_err\':0.0,\n                                       \'dir_err\':0.0,\'late_err\':0.0,\'good\':0.0,\n                                       \'rej_win\':0.0,\'rej_loss\':0.0,\'rej_n\':0})\n\n    for rr in rows:\n        x=dict(rr); p=_v84_json(x.get(\'payload\'))\n        d=str(p.get(\'direction\') or \'NO_TRADE\')\n        if d not in (\'LONG\',\'SHORT\'): continue\n        fam=str(p.get(\'setup_family\') or \'UNKNOWN\')\n        reg=str(p.get(\'regime_bucket\') or \'ADAPTIVE\')\n        ent=str(p.get(\'entry_state\') or \'NORMAL\')\n        asset=str(x.get(\'asset\')); h=str(x.get(\'horizon\'))\n        base=float(p.get(\'learning_weight\',1.0) or 0.0)\n        w=_v84_event_weight(x.get(\'event_ts\'),base)\n        if w<=0: continue\n\n        keys=[\n            (\'EXACT\',asset,h,fam,d,reg,ent),\n            (\'ASSET_FAMILY\',asset,h,fam,d,\'*\',\'*\'),\n            (\'REGIME_FAMILY\',\'*\',h,fam,d,reg,\'*\'),\n            (\'FAMILY\',\'*\',h,fam,d,\'*\',\'*\'),\n        ]\n        for key in keys:\n            z=touch(key)\n            if x.get(\'event_type\')==\'experience_lesson\':\n                z[\'n\']+=1; z[\'w\']+=w\n                if p.get(\'profitable\'): z[\'wins\']+=w\n                z[\'pnl\']+=w*float(p.get(\'actual_pnl_fraction\') or 0.0)\n                label=str(p.get(\'label\') or \'\')\n                if label==\'RIGHT_DIRECTION_STOP_ERROR\': z[\'stop_err\']+=w\n                if label==\'DIRECTION_ERROR\': z[\'dir_err\']+=w\n                if label==\'RIGHT_DIRECTION_LATE_ENTRY\': z[\'late_err\']+=w\n                if label.startswith(\'GOOD_\') or label==\'TACTICAL_WIN_AGAINST_HORIZON\': z[\'good\']+=w\n            else:\n                z[\'rej_n\']+=1\n                if p.get(\'label\')==\'REJECTED_WINNER\': z[\'rej_win\']+=w\n                elif p.get(\'label\')==\'REJECTED_LOSER\': z[\'rej_loss\']+=w\n\n    items=[]\n    for key,z in buckets.items():\n        level,asset,h,fam,d,reg,ent=key\n        en=float(z[\'w\']); n=int(z[\'n\'])\n        pwin=(z[\'wins\']+5.0)/(en+10.0) if en>0 else None\n        rden=z[\'rej_win\']+z[\'rej_loss\']\n        items.append({\n            \'level\':level,\'asset\':asset,\'horizon\':h,\'setup_family\':fam,\'direction\':d,\n            \'regime_bucket\':reg,\'entry_state\':ent,\'n\':n,\'effective_n\':round(en,2),\n            \'posterior_win_rate\':None if pwin is None else round(pwin,4),\n            \'weighted_avg_pnl\':round(z[\'pnl\']/en,6) if en else None,\n            \'stop_error_rate\':round(z[\'stop_err\']/en,4) if en else None,\n            \'direction_error_rate\':round(z[\'dir_err\']/en,4) if en else None,\n            \'late_entry_error_rate\':round(z[\'late_err\']/en,4) if en else None,\n            \'good_execution_rate\':round(z[\'good\']/en,4) if en else None,\n            \'rejected_n\':z[\'rej_n\'],\n            \'rejected_winner_rate\':round(z[\'rej_win\']/rden,4) if rden else None,\n            \'status\':\'WEIGHT_READY\' if en>=EXPERIENCE_MIN_WEIGHT_N else (\n                     \'EXECUTION_READY\' if en>=EXPERIENCE_MIN_EXEC_N else (\n                     \'SIZE_READY\' if en>=EXPERIENCE_MIN_SIZE_N else \'BUILDING\'))\n        })\n    items.sort(key=lambda x:(x[\'level\']!=\'EXACT\',x[\'status\']==\'BUILDING\',-float(x.get(\'effective_n\') or 0)))\n    out={\'status\':\'OK\',\'items\':items,\n         \'thresholds\':{\'size\':EXPERIENCE_MIN_SIZE_N,\'execution\':EXPERIENCE_MIN_EXEC_N,\'weight\':EXPERIENCE_MIN_WEIGHT_N},\n         \'hierarchy\':[\'EXACT\',\'ASSET_FAMILY\',\'REGIME_FAMILY\',\'FAMILY\'],\n         \'principle\':\'exact setup/regime experience dominates; broader pools only shrink small samples\'}\n    setup_memory_board._cache=(time.time(),out)\n    return out\n\n\ndef setup_memory_profile(asset,horizon,row,direction,plan):\n    board=setup_memory_board()\n    fam=_v84_setup_family(row,plan); reg=_v84_regime_bucket(row,plan); ent=_v84_entry_state(row,plan)\n    wanted=[\n        (\'EXACT\',asset,horizon,fam,direction,reg,ent,1.00),\n        (\'ASSET_FAMILY\',asset,horizon,fam,direction,\'*\',\'*\',0.70),\n        (\'REGIME_FAMILY\',\'*\',horizon,fam,direction,reg,\'*\',0.55),\n        (\'FAMILY\',\'*\',horizon,fam,direction,\'*\',\'*\',0.35),\n    ]\n    selected=[]\n    for lvl,a,h,f,d,r,e,shrink in wanted:\n        x=next((q for q in board.get(\'items\',[]) if q.get(\'level\')==lvl and q.get(\'asset\')==a\n                and q.get(\'horizon\')==h and q.get(\'setup_family\')==f and q.get(\'direction\')==d\n                and q.get(\'regime_bucket\')==r and q.get(\'entry_state\')==e),None)\n        if x and float(x.get(\'effective_n\') or 0)>0:\n            selected.append((x,shrink))\n    if not selected:\n        return {\'status\':\'BUILDING\',\'decision_influence\':False,\'setup_family\':fam,\n                \'regime_bucket\':reg,\'entry_state\':ent,\'effective_n\':0.0}\n\n    num=den=0.0; pnl_num=0.0; stop_num=late_num=dir_num=rej_num=rej_den=0.0; eff=0.0\n    evidence=[]\n    for x,shrink in selected:\n        en=float(x.get(\'effective_n\') or 0.0)\n        w=min(40.0,en)*shrink\n        p=x.get(\'posterior_win_rate\')\n        if p is not None: num+=float(p)*w; den+=w\n        if x.get(\'weighted_avg_pnl\') is not None: pnl_num+=float(x[\'weighted_avg_pnl\'])*w\n        stop_num+=float(x.get(\'stop_error_rate\') or 0.0)*w\n        late_num+=float(x.get(\'late_entry_error_rate\') or 0.0)*w\n        dir_num+=float(x.get(\'direction_error_rate\') or 0.0)*w\n        rw=x.get(\'rejected_winner_rate\')\n        if rw is not None: rej_num+=float(rw)*w; rej_den+=w\n        eff=max(eff,en if x.get(\'level\')==\'EXACT\' else en*shrink)\n        evidence.append({\'level\':x.get(\'level\'),\'effective_n\':en,\'pwin\':p})\n    pwin=num/den if den else None\n    level=\'OBSERVE\'\n    if eff>=EXPERIENCE_MIN_WEIGHT_N: level=\'WEIGHT_READY\'\n    elif eff>=EXPERIENCE_MIN_EXEC_N: level=\'EXECUTION_READY\'\n    elif eff>=EXPERIENCE_MIN_SIZE_N: level=\'SIZE_READY\'\n    return {\'status\':level,\'decision_influence\':level!=\'OBSERVE\',\n            \'setup_family\':fam,\'regime_bucket\':reg,\'entry_state\':ent,\n            \'effective_n\':round(eff,2),\'posterior_win_rate\':None if pwin is None else round(pwin,4),\n            \'weighted_avg_pnl\':round(pnl_num/den,6) if den else None,\n            \'stop_error_rate\':round(stop_num/den,4) if den else None,\n            \'late_entry_error_rate\':round(late_num/den,4) if den else None,\n            \'direction_error_rate\':round(dir_num/den,4) if den else None,\n            \'rejected_winner_rate\':round(rej_num/rej_den,4) if rej_den else None,\n            \'evidence\':evidence}\n\n\ndef adaptive_regime_policy(asset,horizon,row,direction,plan,memory):\n    reg=_v84_regime_bucket(row,plan)\n    route=regime_route_for(asset,horizon,row.get(\'regime\'),direction)\n    size=1.0; stop=1.0; confirmation=\'NORMAL\'; add_allowed=True; exit_guard=\'NORMAL\'\n\n    if reg==\'TREND\':\n        size=1.05; stop=1.05; confirmation=\'TREND_CONTINUATION\'; exit_guard=\'STRONG\'\n    elif reg==\'RANGE\':\n        size=0.80; stop=0.95; confirmation=\'RETEST_OR_EDGE\'; add_allowed=False; exit_guard=\'NORMAL\'\n    elif reg==\'HIGH_VOL_TREND\':\n        size=0.75; stop=1.18; confirmation=\'STAGED\'; exit_guard=\'STRONG\'\n    elif reg==\'HIGH_VOL_RANGE\':\n        size=0.65; stop=1.12; confirmation=\'RETEST_ONLY\'; add_allowed=False; exit_guard=\'NORMAL\'\n    elif reg==\'TRANSITION\':\n        size=0.65; stop=1.10; confirmation=\'PROBE_THEN_CONFIRM\'; add_allowed=False; exit_guard=\'STRONG\'\n\n    if route.get(\'decision_influence\'):\n        size*=clip(float(route.get(\'position_multiplier\') or 1.0),0.70,1.20)\n\n    p=(memory or {}).get(\'posterior_win_rate\')\n    if (memory or {}).get(\'decision_influence\') and p is not None:\n        p=float(p)\n        if p>=0.68: size*=1.10\n        elif p<=0.44: size*=0.75\n\n    size=clip(size,0.50,1.25)\n    stop=clip(stop,0.90,V84_STOP_WIDEN_CAP)\n    return {\'regime_bucket\':reg,\'route\':route.get(\'route\'),\'route_status\':route.get(\'status\'),\n            \'decision_influence\':True,\'size_multiplier\':round(size,4),\n            \'stop_multiplier\':round(stop,4),\'confirmation_mode\':confirmation,\n            \'add_allowed\':add_allowed,\'exit_guard\':exit_guard,\n            \'direction_override\':False}\n\n\ndef execution_policy_v84(asset,horizon,row,direction,plan,memory,regime_policy):\n    entry_state=_v84_entry_state(row,plan)\n    p=(memory or {}).get(\'posterior_win_rate\')\n    stop_err=float((memory or {}).get(\'stop_error_rate\') or 0.0)\n    late_err=float((memory or {}).get(\'late_entry_error_rate\') or 0.0)\n    rej=float((memory or {}).get(\'rejected_winner_rate\') or 0.0)\n    eff=float((memory or {}).get(\'effective_n\') or 0.0)\n\n    entry_mode=\'IMMEDIATE_PROBE\'\n    if entry_state==\'LATE\':\n        entry_mode=\'PULLBACK_OR_MIN_PROBE\'\n    elif entry_state==\'RETEST\':\n        entry_mode=\'RETEST_STAGED\'\n    elif entry_state==\'BREAKOUT\':\n        entry_mode=\'BREAKOUT_PROBE_THEN_CONFIRM\'\n    elif entry_state==\'CONFIRMED\':\n        entry_mode=\'CONFIRMED_SCALE\'\n    elif (regime_policy or {}).get(\'confirmation_mode\') in (\'STAGED\',\'PROBE_THEN_CONFIRM\',\'RETEST_ONLY\'):\n        entry_mode=\'REGIME_STAGED\'\n\n    size_mult=float((regime_policy or {}).get(\'size_multiplier\') or 1.0)\n    if eff>=EXPERIENCE_MIN_SIZE_N and p is not None:\n        if float(p)>=0.72: size_mult*=1.20\n        elif float(p)>=0.64: size_mult*=1.10\n        elif float(p)<=0.38: size_mult*=0.55\n        elif float(p)<=0.46: size_mult*=0.75\n    if late_err>=0.25 and entry_state==\'LATE\':\n        size_mult=min(size_mult,0.60)\n    if entry_mode in (\'PULLBACK_OR_MIN_PROBE\',\'REGIME_STAGED\'):\n        size_mult=min(size_mult,0.70)\n    elif entry_mode in (\'RETEST_STAGED\',\'BREAKOUT_PROBE_THEN_CONFIRM\'):\n        size_mult=min(size_mult,0.85)\n    elif entry_mode==\'CONFIRMED_SCALE\' and p is not None and float(p)>=0.60:\n        size_mult=max(size_mult,1.05)\n    mem_status=str((memory or {}).get(\'status\') or \'OBSERVE\')\n    if rej>=0.65 and mem_status in (\'EXECUTION_READY\',\'WEIGHT_READY\'):\n        entry_mode=\'SIGNAL_FIRST_REINFORCED\'\n\n    stop_mult=float((regime_policy or {}).get(\'stop_multiplier\') or 1.0)\n    if mem_status in (\'EXECUTION_READY\',\'WEIGHT_READY\') and stop_err>=0.25:\n        stop_mult*=min(1.20,1.0+0.45*(stop_err-0.20))\n    stop_mult=clip(stop_mult,0.90,V84_STOP_WIDEN_CAP)\n\n    # Exit optimization: protect against single-horizon noise. Hard invalidation remains immediate.\n    reg=(regime_policy or {}).get(\'regime_bucket\')\n    flip_ratio=1.20 if reg==\'TREND\' else 1.30 if reg in (\'HIGH_VOL_TREND\',\'TRANSITION\') else 1.15\n    flip_horizons=2\n    trail_activation_r=1.25 if reg==\'TREND\' else 1.50 if reg==\'HIGH_VOL_TREND\' else 0.90 if reg==\'RANGE\' else 1.10\n    return {\'version\':\'v84\',\'decision_influence\':True,\n            \'entry_mode\':entry_mode,\'size_multiplier\':round(clip(size_mult,0.50,1.30),4),\n            \'stop_distance_multiplier\':round(stop_mult,4),\n            \'add_allowed\':bool((regime_policy or {}).get(\'add_allowed\',True)),\n            \'flip_confirmation_ratio\':flip_ratio,\'flip_confirmation_horizons\':flip_horizons,\n            \'premature_exit_guard\':True,\'hard_invalidation_immediate\':True,\n            \'trail_mode\':\'STRUCTURAL_ONLY\',\'trail_activation_r\':trail_activation_r,\n            \'soft_deterioration_action\':\'HOLD_OR_REDUCE_NOT_EXIT\',\n            \'hard_gate_override\':False}\n\n\ndef apply_v84_execution_to_trade_plan(row,plan,memory,regime_policy,execution_policy):\n    plan=dict(plan or {})\n    plan[\'setup_memory\']=memory\n    plan[\'adaptive_regime_policy\']=regime_policy\n    plan[\'execution_policy\']=execution_policy\n\n    f0=float(plan.get(\'initial_position_fraction\') or 0.0)\n    if f0>0:\n        mult=float(execution_policy.get(\'size_multiplier\') or 1.0)\n        plan[\'initial_position_fraction\']=clip(max(0.05,f0*mult),0.05,1.0)\n    plan[\'experience_entry_mode\']=execution_policy.get(\'entry_mode\')\n\n    # Widen only when experience/regime evidence supports it; never tighten an established structural stop here.\n    sm=float(execution_policy.get(\'stop_distance_multiplier\') or 1.0)\n    stop=plan.get(\'stop_price\'); price=float((row or {}).get(\'price\') or 0.0)\n    if sm>1.0 and stop is not None and price>0 and direction_valid(row):\n        d=str((row or {}).get(\'research_decision\'))\n        try:\n            stop=float(stop); dist=abs(price-stop)\n            if dist>0:\n                nd=min(dist*sm,dist*V84_STOP_WIDEN_CAP)\n                candidate=price-nd if d==\'LONG\' else price+nd\n                if (d==\'LONG\' and candidate<stop) or (d==\'SHORT\' and candidate>stop):\n                    plan[\'experience_original_stop\']=stop\n                    plan[\'stop_price\']=candidate\n                    plan[\'stop_method\']=str(plan.get(\'stop_method\') or \'STRUCTURAL\')+\'+V84_BUFFER\'\n        except Exception:\n            pass\n\n    # Recompute economics honestly after any stop change.\n    try:\n        stop=float(plan.get(\'stop_price\')); price=float((row or {}).get(\'price\') or 0.0)\n        if price>0:\n            dist=abs(price-stop)/price\n            plan[\'stop_distance_pct\']=dist\n            exp=float(plan.get(\'expected_move_pct\') or 0.0)\n            plan[\'expected_to_stop_ratio\']=exp/dist if dist>1e-12 else 999.0\n    except Exception:\n        pass\n\n    plan[\'structural_stop_enforced\']=True\n    return plan\n\n\ndef direction_valid(row):\n    return str((row or {}).get(\'research_decision\') or \'\') in (\'LONG\',\'SHORT\')\n\n\ndef experience_profile_for_trade(asset,horizon,row,direction,plan,tradeability):\n    memory=setup_memory_profile(asset,horizon,row,direction,plan)\n    analog_n=int((tradeability or {}).get(\'raw_n\') or 0)\n    analog_eff=float((tradeability or {}).get(\'effective_n\') or 0.0)\n    analog_p=(tradeability or {}).get(\'positive_trade_probability\')\n    mp=memory.get(\'posterior_win_rate\'); me=float(memory.get(\'effective_n\') or 0.0)\n\n    weighted=[]\n    if analog_p is not None and analog_eff>0: weighted.append((float(analog_p),min(40.0,analog_eff)))\n    if mp is not None and me>0: weighted.append((float(mp),min(40.0,me)))\n    combined=sum(p*w for p,w in weighted)/sum(w for _,w in weighted) if weighted else None\n    effective=max(analog_eff,me)\n    level=\'OBSERVE\'\n    if effective>=EXPERIENCE_MIN_WEIGHT_N: level=\'WEIGHT_READY\'\n    elif effective>=EXPERIENCE_MIN_EXEC_N: level=\'EXECUTION_READY\'\n    elif effective>=EXPERIENCE_MIN_SIZE_N: level=\'SIZE_READY\'\n\n    return {\'status\':level,\'decision_influence\':level!=\'OBSERVE\',\n            \'asset\':asset,\'horizon\':horizon,\'direction\':direction,\n            \'combined_positive_probability\':None if combined is None else round(combined,4),\n            \'analog_raw_n\':analog_n,\'analog_effective_n\':round(analog_eff,2),\n            \'setup_memory_effective_n\':round(me,2),\'setup_memory\':memory,\n            \'hard_gate_override\':False}\n\n\ndef v84_direction_flip_confirmed(x,current_direction):\n    d=str((x or {}).get(\'research_decision\') or \'NO_TRADE\')\n    if d not in (\'LONG\',\'SHORT\') or d==current_direction:\n        return False\n    piv=(x or {}).get(\'impulse_pivot_break\') or {}\n    rev=(x or {}).get(\'tactical_reversal\') or {}\n    if piv.get(\'active\') and str(piv.get(\'direction\') or \'\')==d and float(piv.get(\'probability\') or 0)>=0.76:\n        return True\n    if rev.get(\'active\') and str(rev.get(\'direction\') or \'\')==d and float(rev.get(\'probability\') or 0)>=0.78:\n        return True\n    support=(x or {}).get(\'_uec_direction_support\') or {}\n    ours=float(support.get(d) or 0.0); other=float(support.get(current_direction) or 0.0)\n    hs=(x or {}).get(\'_uec_supporting_horizons\') or []\n    ep=((x or {}).get(\'trade_plan\') or {}).get(\'execution_policy\') or {}\n    ratio=float(ep.get(\'flip_confirmation_ratio\') or 1.20)\n    need=int(ep.get(\'flip_confirmation_horizons\') or 2)\n    return bool(len(hs)>=need and ours>=max(0.01,other)*ratio)\n\n\ndef execution_learning_board():\n    m=setup_memory_board()\n    items=m.get(\'items\') or []\n    exact=[x for x in items if x.get(\'level\')==\'EXACT\']\n    return {\'status\':m.get(\'status\'),\'exact_setups\':len(exact),\n            \'weight_ready\':sum(1 for x in exact if x.get(\'status\')==\'WEIGHT_READY\'),\n            \'execution_ready\':sum(1 for x in exact if x.get(\'status\') in (\'EXECUTION_READY\',\'WEIGHT_READY\')),\n            \'top_setups\':exact[:30],\n            \'principle\':\'direction, entry, stop and exit learn separately; hard safety gates never decay from experience\'}\n\n\ndef learning_index_v2():\n    if not pg_enabled(): return {\'status\':\'POSTGRES_REQUIRED\'}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT COALESCE(payload->>\'model_version\',\'UNKNOWN\') model_version,\n                                     COUNT(*) n,\n                                     COUNT(*) FILTER(WHERE profitable) wins,\n                                     COALESCE(AVG(net_pnl_rub),0) avg_pnl,\n                                     COALESCE(SUM(net_pnl_rub),0) total_pnl\n                              FROM paper_trades WHERE status=\'CLOSED\' GROUP BY 1""").fetchall()\n        by={str(r[\'model_version\']):dict(r) for r in rows}\n        def norm(x):\n            if not x: return {\'n\':0,\'wins\':0,\'win_rate\':None,\'avg_pnl\':None,\'total_pnl\':None}\n            n=int(x.get(\'n\') or 0); w=int(x.get(\'wins\') or 0)\n            return {\'n\':n,\'wins\':w,\'win_rate\':w/n if n else None,\n                    \'avg_pnl\':float(x.get(\'avg_pnl\') or 0.0),\'total_pnl\':float(x.get(\'total_pnl\') or 0.0)}\n        base=norm(by.get(\'veritas-portfolio-v6-v80-unified-execution\'))\n        cur=norm(by.get(\'veritas-portfolio-v8.2-v84-audited-execution\'))\n        measurable=cur[\'n\']>=20 and base[\'n\']>=8\n        gate=None\n        if measurable:\n            gate=bool(cur[\'win_rate\'] is not None and base[\'win_rate\'] is not None\n                      and cur[\'win_rate\']>=base[\'win_rate\'] and cur[\'avg_pnl\']>=base[\'avg_pnl\'])\n        return {\'status\':\'MEASURABLE\' if measurable else \'BUILDING\',\n                \'frozen_baseline\':\'veritas-portfolio-v6-v80-unified-execution\',\n                \'baseline\':base,\'v84\':cur,\'quality_gate_pass\':gate,\n                \'promotion_rule\':\'win rate must improve or hold while average P&L does not deteriorate\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'error\':f\'{type(ex).__name__}: {ex}\'}\n\n\n# =========================\n# VERITAS v84.2 AUDITED OVERRIDES\n# =========================\n# These definitions intentionally override earlier v84 helpers.\n# They keep the conceptual v81-v84 layers while correcting latency,\n# learning maturity, sizing consistency, data-source resilience and I/O.\n\n# ---------- durable event writes without a new TLS/DB handshake per event ----------\n_v842_pg_event_local = threading.local()\n\ndef _v842_pg_event_conn():\n    if psycopg is None:\n        raise RuntimeError(\'PSYCOPG_NOT_INSTALLED\')\n    c=getattr(_v842_pg_event_local,\'conn\',None)\n    if c is not None and not getattr(c,\'closed\',True):\n        return c\n    c=psycopg.connect(DATABASE_URL,autocommit=True,row_factory=dict_row)\n    _v842_pg_event_local.conn=c\n    return c\n\ndef pg_event(event_type, entity_key, payload, asset=None, horizon=None, event_ts=None):\n    if not pg_enabled():\n        return False\n    ts=event_ts or now()\n    key=f\'{event_type}:{entity_key}\'\n    args=(key,entity_key,event_type,ts,asset,horizon,\n          json.dumps(payload,ensure_ascii=False),VERSION)\n    last=None\n    for _attempt in range(2):\n        try:\n            c=_v842_pg_event_conn()\n            row=c.execute("""INSERT INTO ledger_events\n              (event_key,entity_key,event_type,event_ts,asset,horizon,payload,model_version)\n              VALUES(%s,%s,%s,%s,%s,%s,%s::jsonb,%s)\n              ON CONFLICT(event_key) DO NOTHING\n              RETURNING id""",args).fetchone()\n            return bool(row)\n        except Exception as ex:\n            last=ex\n            try:\n                c=getattr(_v842_pg_event_local,\'conn\',None)\n                if c is not None: c.close()\n            except Exception:\n                pass\n            _v842_pg_event_local.conn=None\n    raise last\n\n# ---------- fast decision-memory index: scan only matching horizon ----------\ndef _v842_vectors_by_horizon():\n    vecs=_decision_memory_vectors()\n    token=(id(vecs),len(vecs))\n    cache=getattr(_v842_vectors_by_horizon,\'_cache\',None)\n    if cache and cache[0]==token:\n        return cache[1]\n    out={h:[] for h in HORIZONS}\n    for r,hv in vecs:\n        h=str(r.get(\'horizon\') or \'\')\n        if h in out:\n            out[h].append((r,hv))\n    _v842_vectors_by_horizon._cache=(token,out)\n    return out\n\ndef tradeability_analog_stats(asset,horizon,f,direction):\n    if direction not in (\'LONG\',\'SHORT\') or not pg_enabled():\n        return {\'status\':\'NO_DIRECTION\',\'positive_trade_probability\':None,\n                \'raw_n\':0,\'effective_n\':0.0,\'decision_influence\':False}\n    cur=_state_from_features(f)\n    cv=_normalized_state_vector(cur)\n    weights=[1.1,0.9,1.0,1.1,1.35,0.45,0.9,0.9,1.05,1.15,0.35,0.75,0.55]\n    candidates=[]\n    now_dt=datetime.now(timezone.utc)\n    for r,hv in _v842_vectors_by_horizon().get(horizon,()):\n        if r.get(\'forward_return\') is None:\n            continue\n        dist=math.sqrt(sum(w*(a-b)*(a-b) for w,a,b in zip(weights,cv,hv))/max(1e-12,sum(weights)))\n        if str(r.get(\'regime\') or \'\') != str(cur.get(\'regime\') or \'\'):\n            dist += 0.12\n        dist *= 0.86 if r.get(\'asset\')==asset else 1.04\n        candidates.append((dist,r))\n    candidates.sort(key=lambda z:z[0])\n    nearest=candidates[:ANALOG_NEIGHBORS]\n    if not nearest:\n        return {\'status\':\'BUILDING\',\'positive_trade_probability\':None,\n                \'raw_n\':0,\'effective_n\':0.0,\'decision_influence\':False}\n    wh=wr=sw=0.0\n    signed=[]; favorable=[]; adverse=[]; compact=[]\n    for dist,r in nearest:\n        ts=r.get(\'event_ts\')\n        if isinstance(ts,str):\n            try: ts=datetime.fromisoformat(ts.replace(\'Z\',\'+00:00\'))\n            except Exception: ts=None\n        if ts is not None and ts.tzinfo is None:\n            ts=ts.replace(tzinfo=timezone.utc)\n        age=max(0.0,(now_dt-ts).total_seconds()/86400.0) if ts is not None else 0.0\n        w=math.exp(-2.2*dist)*math.exp(-math.log(2.0)*age/240.0)\n        if r.get(\'asset\')==asset:\n            w*=1.10\n        fr=float(r[\'forward_return\'])\n        sr=fr if direction==\'LONG\' else -fr\n        sw+=w\n        wh+=w*(1.0 if sr>0 else 0.0)\n        wr+=w*sr\n        signed.append(sr)\n        mfe=r.get(\'mfe\'); mae=r.get(\'mae\')\n        fav=(float(mfe) if direction==\'LONG\' else -float(mae)) if (mfe is not None and mae is not None) else None\n        adv=(-float(mae) if direction==\'LONG\' else float(mfe)) if (mfe is not None and mae is not None) else None\n        if fav is not None: favorable.append(max(0.0,fav))\n        if adv is not None: adverse.append(max(0.0,adv))\n        if len(compact)<6:\n            compact.append({\'asset\':r.get(\'asset\'),\'ts\':r.get(\'event_ts\'),\n                            \'distance\':round(dist,4),\'forward_signed_return\':round(sr,6)})\n    prior=TRADEABILITY_BETA_PRIOR\n    alpha=prior+wh\n    beta=prior+max(0.0,sw-wh)\n    post=alpha/(alpha+beta)\n    var=(alpha*beta)/(((alpha+beta)**2)*(alpha+beta+1.0)) if alpha+beta>0 else 0.0\n    sd=math.sqrt(max(0.0,var))\n    lower=max(0.0,post-1.2815515655*sd)\n    upper=min(1.0,post+1.2815515655*sd)\n    raw_n=len(nearest)\n    measurable=raw_n>=TRADEABILITY_MIN_RAW_N and sw>=TRADEABILITY_MIN_EFFECTIVE_N\n    if not measurable: label=\'BUILDING\'\n    elif post>=TRADEABILITY_SUPPORT_P and (wr/sw if sw else 0)>0: label=\'SUPPORTED\'\n    elif post<=TRADEABILITY_WEAK_P: label=\'WEAK\'\n    else: label=\'NEUTRAL\'\n    return {\'status\':label,\n            \'positive_trade_probability\':round(post,4) if measurable else None,\n            \'shadow_posterior\':round(post,4),\n            \'probability_band_80\':[round(lower,4),round(upper,4)],\n            \'raw_n\':raw_n,\'effective_n\':round(sw,2),\n            \'weighted_avg_signed_return\':round(wr/sw,6) if sw else None,\n            \'median_signed_return\':round(_quantile_simple(signed,0.5),6) if signed else None,\n            \'median_favorable_excursion\':_quantile_simple(favorable,0.5),\n            \'p80_adverse_excursion\':_quantile_simple(adverse,0.80),\n            \'same_horizon_cross_asset_transfer\':True,\'nearest\':compact,\n            \'decision_influence\':False,\n            \'definition\':\'Nearest pre-decision states, horizon-indexed; Bayesian shrinkage; current direction applied to realized forward return.\'}\n\n# ---------- bounded direction prior: only material analog skew participates ----------\ndef experience_direction_prior(asset,horizon,f):\n    try:\n        row=tradeability_analog_stats(asset,horizon,f,\'LONG\')\n        n=int(row.get(\'raw_n\') or 0)\n        en=float(row.get(\'effective_n\') or 0.0)\n        p=row.get(\'positive_trade_probability\')\n        prior_min_n=max(40,int(TRADEABILITY_MIN_RAW_N))\n        prior_min_eff=max(20.0,float(TRADEABILITY_MIN_EFFECTIVE_N)*2.0)\n        if p is None or n<prior_min_n or en<prior_min_eff:\n            return {\'status\':\'BUILDING\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                    \'raw_n\':n,\'effective_n\':en,\'p_long\':p,\n                    \'required_raw_n\':prior_min_n,\'required_effective_n\':prior_min_eff}\n        p=float(p)\n        if abs(p-0.50)<0.08:\n            adj=0.0\n            active=False\n        else:\n            adj=clip((p-0.50)*0.12,-V84_DIRECTION_PRIOR_CAP,V84_DIRECTION_PRIOR_CAP)\n            active=abs(adj)>1e-9\n        return {\'status\':\'ACTIVE\' if active else \'NEUTRAL\',\n                \'decision_influence\':active,\'score_adjustment\':round(adj,5),\n                \'raw_n\':n,\'effective_n\':round(en,2),\'p_long\':round(p,4),\n                \'principle\':\'small analog prior only; never overrides hard gates or a strong current signal\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                \'error\':f\'{type(ex).__name__}: {ex}\'}\n\n# ---------- memory is built only in background ----------\n_v842_original_setup_memory_board=setup_memory_board\n\ndef setup_memory_board(force=False):\n    cache=getattr(setup_memory_board,\'_cache\',None)\n    if cache and not force:\n        return cache[1]\n    if not force:\n        return {\'status\':\'BACKGROUND_PENDING\',\'items\':[],\n                \'decision_influence\':False,\n                \'principle\':\'fast path reads only a precomputed background experience snapshot\'}\n    out=_v842_original_setup_memory_board(force=True)\n    setup_memory_board._cache=(time.time(),out)\n    return out\n\ndef _v842_memory_lookup(board):\n    items=board.get(\'items\') or []\n    token=(id(items),len(items))\n    cache=getattr(_v842_memory_lookup,\'_cache\',None)\n    if cache and cache[0]==token:\n        return cache[1]\n    idx={}\n    for x in items:\n        k=(x.get(\'level\'),x.get(\'asset\'),x.get(\'horizon\'),x.get(\'setup_family\'),\n           x.get(\'direction\'),x.get(\'regime_bucket\'),x.get(\'entry_state\'))\n        idx[k]=x\n    _v842_memory_lookup._cache=(token,idx)\n    return idx\n\ndef setup_memory_profile(asset,horizon,row,direction,plan):\n    fam=_v84_setup_family(row,plan)\n    reg=_v84_regime_bucket(row,plan)\n    ent=_v84_entry_state(row,plan)\n    if direction not in (\'LONG\',\'SHORT\'):\n        return {\'status\':\'BUILDING\',\'decision_influence\':False,\'setup_family\':fam,\n                \'regime_bucket\':reg,\'entry_state\':ent,\'effective_n\':0.0,\n                \'exact_effective_n\':0.0}\n    board=setup_memory_board()\n    idx=_v842_memory_lookup(board)\n    wanted=[\n        ((\'EXACT\',asset,horizon,fam,direction,reg,ent),1.00),\n        ((\'ASSET_FAMILY\',asset,horizon,fam,direction,\'*\',\'*\'),0.45),\n        ((\'REGIME_FAMILY\',\'*\',horizon,fam,direction,reg,\'*\'),0.25),\n        ((\'FAMILY\',\'*\',horizon,fam,direction,\'*\',\'*\'),0.12),\n    ]\n    selected=[]\n    for k,shrink in wanted:\n        x=idx.get(k)\n        if x and float(x.get(\'effective_n\') or 0)>0:\n            selected.append((x,shrink))\n    if not selected:\n        return {\'status\':\'BUILDING\',\'decision_influence\':False,\'setup_family\':fam,\n                \'regime_bucket\':reg,\'entry_state\':ent,\'effective_n\':0.0,\n                \'exact_effective_n\':0.0}\n\n    num=den=0.0\n    pnl_num=stop_num=late_num=dir_num=rej_num=rej_den=0.0\n    exact_en=0.0\n    support_eff=0.0\n    evidence=[]\n    for x,shrink in selected:\n        en=float(x.get(\'effective_n\') or 0.0)\n        lvl=str(x.get(\'level\') or \'\')\n        if lvl==\'EXACT\':\n            exact_en=en\n            maturity_part=en\n        elif lvl==\'ASSET_FAMILY\':\n            maturity_part=min(12.0,en*0.30)\n        elif lvl==\'REGIME_FAMILY\':\n            maturity_part=min(8.0,en*0.20)\n        else:\n            maturity_part=min(4.0,en*0.08)\n        if lvl!=\'EXACT\':\n            support_eff+=maturity_part\n        w=min(40.0,en)*shrink\n        p=x.get(\'posterior_win_rate\')\n        if p is not None:\n            num+=float(p)*w; den+=w\n        if x.get(\'weighted_avg_pnl\') is not None:\n            pnl_num+=float(x[\'weighted_avg_pnl\'])*w\n        stop_num+=float(x.get(\'stop_error_rate\') or 0.0)*w\n        late_num+=float(x.get(\'late_entry_error_rate\') or 0.0)*w\n        dir_num+=float(x.get(\'direction_error_rate\') or 0.0)*w\n        rw=x.get(\'rejected_winner_rate\')\n        if rw is not None:\n            rej_num+=float(rw)*w; rej_den+=w\n        evidence.append({\'level\':lvl,\'effective_n\':round(en,2),\'pwin\':p})\n    maturity=exact_en+support_eff\n    pwin=num/den if den else None\n\n    # Broad experience may affect sizing, but execution/weight maturity requires own exact history.\n    if exact_en>=20 and maturity>=40:\n        level=\'WEIGHT_READY\'\n    elif exact_en>=8 and maturity>=20:\n        level=\'EXECUTION_READY\'\n    elif maturity>=EXPERIENCE_MIN_SIZE_N:\n        level=\'SIZE_READY\'\n    else:\n        level=\'OBSERVE\'\n    return {\'status\':level,\'decision_influence\':level!=\'OBSERVE\',\n            \'setup_family\':fam,\'regime_bucket\':reg,\'entry_state\':ent,\n            \'effective_n\':round(maturity,2),\'exact_effective_n\':round(exact_en,2),\n            \'transfer_effective_n\':round(support_eff,2),\n            \'posterior_win_rate\':None if pwin is None else round(pwin,4),\n            \'weighted_avg_pnl\':round(pnl_num/den,6) if den else None,\n            \'stop_error_rate\':round(stop_num/den,4) if den else None,\n            \'late_entry_error_rate\':round(late_num/den,4) if den else None,\n            \'direction_error_rate\':round(dir_num/den,4) if den else None,\n            \'rejected_winner_rate\':round(rej_num/rej_den,4) if rej_den else None,\n            \'evidence\':evidence,\n            \'transfer_policy\':\'broad analogs may size; exact history required for execution/weight maturity\'}\n\n# ---------- single authoritative execution target; no double sizing ----------\ndef apply_v84_execution_to_trade_plan(row,plan,memory,regime_policy,execution_policy):\n    plan=dict(plan or {})\n    plan[\'setup_memory\']=memory\n    plan[\'adaptive_regime_policy\']=regime_policy\n    plan[\'execution_policy\']=execution_policy\n\n    f0=float(plan.get(\'initial_position_fraction\') or 0.0)\n    mult=float(execution_policy.get(\'size_multiplier\') or 1.0)\n    plan[\'v84_raw_initial_fraction\']=f0\n    if f0>0:\n        plan[\'v84_target_fraction\']=clip(max(0.05,f0*mult),0.05,1.0)\n    else:\n        plan[\'v84_target_fraction\']=0.0\n    plan[\'experience_entry_mode\']=execution_policy.get(\'entry_mode\')\n\n    sm=float(execution_policy.get(\'stop_distance_multiplier\') or 1.0)\n    stop=plan.get(\'stop_price\')\n    price=float((row or {}).get(\'price\') or 0.0)\n    if sm>1.0 and stop is not None and price>0 and direction_valid(row):\n        d=str((row or {}).get(\'research_decision\'))\n        try:\n            stop=float(stop); dist=abs(price-stop)\n            if dist>0:\n                nd=min(dist*sm,dist*V84_STOP_WIDEN_CAP)\n                candidate=price-nd if d==\'LONG\' else price+nd\n                if (d==\'LONG\' and candidate<stop) or (d==\'SHORT\' and candidate>stop):\n                    plan[\'experience_original_stop\']=stop\n                    plan[\'stop_price\']=candidate\n                    plan[\'stop_method\']=str(plan.get(\'stop_method\') or \'STRUCTURAL\')+\'+V84_BUFFER\'\n        except Exception:\n            pass\n\n    try:\n        stop=float(plan.get(\'stop_price\'))\n        price=float((row or {}).get(\'price\') or 0.0)\n        if price>0:\n            dist=abs(price-stop)/price\n            plan[\'stop_distance_pct\']=dist\n            exp=float(plan.get(\'expected_move_pct\') or 0.0)\n            plan[\'expected_to_stop_ratio\']=exp/dist if dist>1e-12 else 999.0\n    except Exception:\n        pass\n    plan[\'structural_stop_enforced\']=True\n    plan[\'sizing_authority\']=\'DECISION_LAYER_TARGET_THEN_PORTFOLIO_RISK\'\n    return plan\n\n# ---------- faster crypto derivatives ----------\ndef derivatives(symbol):\n    try:\n        base=\'https://fapi.binance.com\'\n        jobs={\n            \'premium\':(base+\'/fapi/v1/premiumIndex\',{\'symbol\':symbol}),\n            \'oi\':(base+\'/fapi/v1/openInterest\',{\'symbol\':symbol}),\n            \'oi_hist\':(base+\'/futures/data/openInterestHist\',{\'symbol\':symbol,\'period\':\'1h\',\'limit\':25}),\n            \'taker\':(base+\'/futures/data/takerlongshortRatio\',{\'symbol\':symbol,\'period\':\'1h\',\'limit\':24}),\n            \'gls\':(base+\'/futures/data/globalLongShortAccountRatio\',{\'symbol\':symbol,\'period\':\'1h\',\'limit\':24}),\n        }\n        out={}\n        with ThreadPoolExecutor(max_workers=3,thread_name_prefix=\'veritas-deriv\') as pool:\n            fut={k:pool.submit(get_json,u,p) for k,(u,p) in jobs.items()}\n            for k,f in fut.items():\n                out[k]=f.result()\n        premium=out[\'premium\']; oi=out[\'oi\']; oi_hist=out[\'oi_hist\']; taker=out[\'taker\']; gls=out[\'gls\']\n        oi_now=float(oi[\'openInterest\'])\n        oi0=float(oi_hist[0][\'sumOpenInterest\']) if oi_hist else oi_now\n        oi_change=oi_now/oi0-1 if oi0 else 0\n        taker_ratio=float(taker[-1][\'buySellRatio\']) if taker else 1.0\n        long_short=float(gls[-1][\'longShortRatio\']) if gls else 1.0\n        mark=float(premium[\'markPrice\']); index=float(premium[\'indexPrice\'])\n        obs=now()\n        _set_source_quality([_source_row(\'Binance derivatives\',\'crypto derivatives\',\'live context\',\n                                        obs,0,\'OK\',\'parallel public funding/mark/OI/taker/account ratios\',\'Binance\')])\n        cur=\'BTC\' if symbol.startswith(\'BTC\') else \'ETH\' if symbol.startswith(\'ETH\') else None\n        opt=deribit_options_context(cur) if cur else {\'ok\':False,\'status\':\'not_applicable\',\'decision_influence\':False}\n        return {\'ok\':True,\'funding\':float(premium[\'lastFundingRate\']),\'mark\':mark,\'index\':index,\n                \'basis\':mark/index-1 if index else 0,\'open_interest\':oi_now,\'oi_change_24h\':oi_change,\n                \'taker_buy_sell_ratio\':taker_ratio,\'global_long_short_ratio\':long_short,\n                \'observed_at\':obs,\'options_shadow\':opt}\n    except Exception as e:\n        return {\'ok\':False,\'error\':f\'{type(e).__name__}: {e}\'}\n\n# ---------- faster futures source collection ----------\ndef _yahoo_research_futures_market(asset,yahoo_symbol,proxy_symbol,policy_key,source_name):\n    with ThreadPoolExecutor(max_workers=3,thread_name_prefix=f\'veritas-{asset.lower()}\') as pool:\n        f5=pool.submit(_yahoo_series,yahoo_symbol,\'5d\',\'5m\',True)\n        f1=pool.submit(_yahoo_series,yahoo_symbol,\'3mo\',\'1h\',True)\n        fp=pool.submit(_yahoo_series,proxy_symbol,\'5d\',\'5m\',True)\n        bars5,_=f5.result()\n        bars1h,_=f1.result()\n        try: pr,_=fp.result()\n        except Exception: pr=[]\n    if len(bars1h)<200:\n        raise RuntimeError(f\'INSUFFICIENT_{asset}_HOURLY_BARS {len(bars1h)}\')\n    last=bars5[-1] if bars5 else bars1h[-1]\n    price=float(last[\'close\'])\n    observed=datetime.fromtimestamp(last[\'ts\'],tz=timezone.utc).isoformat()\n    closes=[float(x[\'close\']) for x in bars1h[-240:]]\n    highs=[float(x[\'high\']) for x in bars1h[-240:]]\n    lows=[float(x[\'low\']) for x in bars1h[-240:]]\n    vols=[float(x.get(\'volume\') or 0) for x in bars1h[-240:]]\n    taker=[v*0.5 for v in vols]\n    rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]\n    market_open=_futures_market_open_from_age(observed)\n    age=_age_seconds(observed)\n    delay=DATA_SOURCE_POLICY[policy_key][\'documented_delay_sec\']\n    proxy_note=\'not checked\'; proxy_obs=None\n    if pr:\n        proxy_obs=datetime.fromtimestamp(pr[-1][\'ts\'],tz=timezone.utc).isoformat()\n        if len(pr)>=5 and len(bars5)>=5:\n            r1=float(bars5[-1][\'close\'])/float(bars5[-5][\'close\'])-1\n            r2=float(pr[-1][\'close\'])/float(pr[-5][\'close\'])-1\n            proxy_note=f\'4-bar directional proxy: primary={r1:.3%}, proxy={r2:.3%}\'\n    gate=bool(market_open and age is not None and age<=DELAYED_FUTURES_MAX_AGE_SECONDS)\n    quality=[\n      _source_row(source_name,f\'{asset} futures\',\'primary research delayed\',observed,delay,\n                  \'DELAYED_CONTEXT\' if gate else \'STALE_OR_CLOSED\',\n                  DATA_SOURCE_POLICY[policy_key][\'commercial_note\'],\'Yahoo\'),\n      _source_row(f\'{proxy_symbol} proxy\',f\'{asset} proxy\',\'verification proxy\',proxy_obs,0,\n                  \'OK\' if proxy_obs else \'NOT_OBSERVED_YET\',proxy_note,\'Yahoo\')]\n    _set_source_quality(quality)\n    return {\'asset\':asset,\'price\':price,\'secondary_price\':None,\'coinbase_price\':None,\n            \'source_divergence\':0.0,\'closes\':closes,\'highs\':highs,\'lows\':lows,\'vols\':vols,\n            \'intraday_bars\':bars5,\'taker_buy\':taker,\'returns\':rets,\n            \'binance_close_time_ms\':int(last[\'ts\']*1000),\'observed_at\':observed,\n            \'source_gate_pass\':gate,\'market_open\':market_open,\'source_quality\':quality,\n            \'data_latency_class\':\'DELAYED_RESEARCH\',\'verification_mode\':\'directional_proxy_only\',\n            \'source_names\':{\'primary\':source_name,\'secondary\':f\'{proxy_symbol} directional proxy\'}}\n\n# ---------- NDX parallel fetch + fail-closed source fallback ----------\n_v842_ndx_lock=threading.Lock()\n_v842_ndx_last_good=None\n\ndef _v842_ndx_cached_fallback(reason):\n    global _v842_ndx_last_good\n    with _v842_ndx_lock:\n        z=_v842_ndx_last_good\n    if not z:\n        return None\n    out=dict(z)\n    primary_age=_age_seconds(out.get(\'observed_at\'))\n    fresh=bool(primary_age is not None and primary_age<=NDX_MAX_PRIMARY_AGE_SECONDS)\n    out[\'source_gate_pass\']=bool(out.get(\'source_gate_pass\') and fresh and _us_rth_now())\n    out[\'market_open\']=_us_rth_now()\n    out[\'verification_mode\']=\'LAST_GOOD_FAILSAFE\'\n    out[\'data_latency_class\']=\'FAILSAFE_CACHE\'\n    out[\'fallback_reason\']=reason\n    q=list(out.get(\'source_quality\') or [])\n    q.append(_source_row(\'VERITAS last-good NDX cache\',\'US index / NDX\',\'failsafe only\',\n                         out.get(\'observed_at\'),0,\'OK\' if out[\'source_gate_pass\'] else \'STALE_FAIL_CLOSED\',\n                         f\'fallback after {reason}; trading gate remains freshness-bound\',\'VERITAS\'))\n    out[\'source_quality\']=q\n    return out\n\ndef _ndx_market():\n    global _v842_ndx_last_good\n    jobs={}\n    try:\n        with ThreadPoolExecutor(max_workers=5,thread_name_prefix=\'veritas-ndx\') as pool:\n            jobs[\'ndx1m\']=pool.submit(_yahoo_series,\'%5ENDX\',\'1d\',\'1m\',False)\n            jobs[\'ndx1h\']=pool.submit(_yahoo_series,\'%5ENDX\',\'3mo\',\'1h\',False)\n            jobs[\'qqq1h\']=pool.submit(_yahoo_series,\'QQQ\',\'3mo\',\'1h\',False)\n            jobs[\'qqq1m\']=pool.submit(_yahoo_series,\'QQQ\',\'1d\',\'1m\',True)\n            jobs[\'daily\']=pool.submit(_yahoo_series,\'%5ENDX\',NDX_LONG_HISTORY_RANGE,\'1d\',False)\n            jobs[\'qqq5m\']=pool.submit(_yahoo_series,\'QQQ\',\'5d\',\'5m\',True)\n            jobs[\'nas\']=pool.submit(_nasdaq_ndx_quote)\n            ndx1m,_=jobs[\'ndx1m\'].result()\n            ndx1h,_=jobs[\'ndx1h\'].result()\n            qqq1h,_=jobs[\'qqq1h\'].result()\n            qqq1m,_=jobs[\'qqq1m\'].result()\n            ndxdaily,_=jobs[\'daily\'].result()\n            try: qqq5m,_=jobs[\'qqq5m\'].result()\n            except Exception: qqq5m=[]\n            try: nas=jobs[\'nas\'].result()\n            except Exception as ex:\n                nas={\'price\':None,\'observed_at\':now(),\'exchange_timestamp_utc\':None,\n                     \'exchange_timestamp\':None,\'is_real_time\':None,\'error\':f\'{type(ex).__name__}: {ex}\'}\n        if not ndx1m or len(ndx1h)<200 or not qqq1h:\n            raise RuntimeError(\'NDX_REQUIRED_YAHOO_SERIES_UNAVAILABLE\')\n        pbar=ndx1m[-1]; price=float(pbar[\'close\'])\n        pts=datetime.fromtimestamp(pbar[\'ts\'],tz=timezone.utc).isoformat()\n        sec=float(nas[\'price\']) if nas.get(\'price\') is not None else None\n        div=(abs(price-sec)/((price+sec)/2)) if sec is not None and (price+sec)!=0 else None\n        qmap={int(x[\'ts\']//3600):x for x in qqq1h}\n        closes=[]; highs=[]; lows=[]; vols=[]; taker=[]\n        for x in ndx1h[-240:]:\n            closes.append(x[\'close\']); highs.append(x[\'high\']); lows.append(x[\'low\'])\n            q=qmap.get(int(x[\'ts\']//3600)); vv=float(q[\'volume\']) if q else 0.0\n            vols.append(vv); taker.append(vv*0.5)\n        rets=[closes[i]/closes[i-1]-1 for i in range(1,len(closes))]\n        rth=_us_rth_now()\n        age=_age_seconds(pts)\n        nas_age=_age_seconds(nas.get(\'exchange_timestamp_utc\')) if nas.get(\'exchange_timestamp_utc\') else None\n        nas_ok=bool(sec is not None and (nas_age is None or nas_age<=300))\n        gate=bool(rth and age is not None and age<=NDX_MAX_PRIMARY_AGE_SECONDS and\n                  nas_ok and div is not None and div<=NDX_MAX_SOURCE_DIVERGENCE)\n        quality=[\n          _source_row(\'Yahoo Nasdaq GIDS\',\'US index / NDX\',\'primary live candidate\',pts,0,\n                      \'OK\' if rth and age is not None and age<=NDX_MAX_PRIMARY_AGE_SECONDS else (\'SESSION_CLOSED\' if not rth else \'STALE\'),\n                      DATA_SOURCE_POLICY[\'yahoo_nasdaq_gids\'][\'commercial_note\'],\'Yahoo/ICE\'),\n          _source_row(\'Nasdaq public index\',\'US index / NDX\',\'verification\',\n                      nas.get(\'exchange_timestamp_utc\') or nas.get(\'observed_at\'),60,\n                      \'OK\' if nas_ok and div is not None and div<=NDX_MAX_SOURCE_DIVERGENCE else \'FAIL_CLOSED\',\n                      f\'price divergence={div}; raw_ts={nas.get("exchange_timestamp")}; error={nas.get("error")}\',\n                      \'Nasdaq\'),\n          _source_row(\'Yahoo QQQ\',\'US ETF proxy\',\'volume proxy\',\n                      datetime.fromtimestamp(qqq1m[-1][\'ts\'],tz=timezone.utc).isoformat() if qqq1m else None,\n                      0,\'OK\' if qqq1m else \'FAIL\',\'QQQ volume proxy, not NDX price\',\'Yahoo/ICE\')]\n        out={\'asset\':\'NDX\',\'price\':price,\'coinbase_price\':sec,\'secondary_price\':sec,\n             \'source_divergence\':div if div is not None else 0.0,\n             \'closes\':closes,\'highs\':highs,\'lows\':lows,\'vols\':vols,\'taker_buy\':taker,\'returns\':rets,\n             \'binance_close_time_ms\':int(pbar[\'ts\']*1000),\'observed_at\':pts,\n             \'source_gate_pass\':gate,\'market_open\':rth,\'source_quality\':quality,\n             \'qqq_price\':qqq1m[-1][\'close\'] if qqq1m else None,\n             \'intraday_bars\':ndx1m,\'daily_bars\':ndxdaily,\'volume_intraday_bars\':qqq5m,\n             \'verification_mode\':\'DIRECT_DUAL_SOURCE\' if nas_ok else \'PRIMARY_ONLY_FAIL_CLOSED\'}\n        _set_source_quality(quality)\n        with _v842_ndx_lock:\n            _v842_ndx_last_good=dict(out)\n        if not nas_ok:\n            emit(\'ndx_verification_fail_closed\',error=nas.get(\'error\'),price=price)\n        return out\n    except Exception as ex:\n        fb=_v842_ndx_cached_fallback(f\'{type(ex).__name__}:{ex}\')\n        if fb is not None:\n            emit(\'ndx_last_good_fallback\',reason=f\'{type(ex).__name__}:{ex}\',\n                 source_gate_pass=fb.get(\'source_gate_pass\'))\n            return fb\n        raise\n\n# ---------- parallel crypto market + derivatives ----------\n_v842_original_fetch_asset_bundle=_fetch_asset_bundle\n\ndef _fetch_asset_bundle(symbol,asset,cb_product):\n    if asset in CRYPTO_ASSETS:\n        t0=time.time()\n        with ThreadPoolExecutor(max_workers=2,thread_name_prefix=f\'veritas-{asset.lower()}\') as pool:\n            fr=pool.submit(market,symbol,cb_product)\n            fd=pool.submit(derivatives,symbol)\n            raw=fr.result(); deriv=fd.result()\n        return {\'symbol\':symbol,\'asset\':asset,\'cb_product\':cb_product,\'raw\':raw,\'deriv\':deriv,\n                \'elapsed_seconds\':time.time()-t0,\'error\':None}\n    return _v842_original_fetch_asset_bundle(symbol,asset,cb_product)\n\n# Keep outer asset fanout conservative; inner provider calls now run in parallel.\nFAST_LOOP_MARKET_WORKERS=max(4,min(5,FAST_LOOP_MARKET_WORKERS))\n\n\ndef learning_index_v2():\n    if not pg_enabled():\n        return {\'status\':\'POSTGRES_REQUIRED\'}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT COALESCE(payload->>\'model_version\',\'UNKNOWN\') model_version,\n                                     COUNT(*) n,\n                                     COUNT(*) FILTER(WHERE profitable) wins,\n                                     COALESCE(AVG(net_pnl_rub),0) avg_pnl,\n                                     COALESCE(SUM(net_pnl_rub),0) total_pnl,\n                                     COALESCE(SUM(net_pnl_rub) FILTER(WHERE net_pnl_rub>0),0) gross_profit,\n                                     COALESCE(ABS(SUM(net_pnl_rub) FILTER(WHERE net_pnl_rub<0)),0) gross_loss,\n                                     COALESCE(AVG(net_pnl_rub) FILTER(WHERE net_pnl_rub>0),0) avg_win,\n                                     COALESCE(ABS(AVG(net_pnl_rub) FILTER(WHERE net_pnl_rub<0)),0) avg_loss\n                              FROM paper_trades\n                              WHERE status=\'CLOSED\'\n                              GROUP BY 1""").fetchall()\n        by={str(r[\'model_version\']):dict(r) for r in rows}\n        def norm(x):\n            if not x:\n                return {\'n\':0,\'wins\':0,\'win_rate\':None,\'avg_pnl\':None,\'total_pnl\':None,\n                        \'profit_factor\':None,\'payoff_ratio\':None}\n            n=int(x.get(\'n\') or 0); w=int(x.get(\'wins\') or 0)\n            gp=float(x.get(\'gross_profit\') or 0.0); gl=float(x.get(\'gross_loss\') or 0.0)\n            aw=float(x.get(\'avg_win\') or 0.0); al=float(x.get(\'avg_loss\') or 0.0)\n            return {\'n\':n,\'wins\':w,\'win_rate\':w/n if n else None,\n                    \'avg_pnl\':float(x.get(\'avg_pnl\') or 0.0),\n                    \'total_pnl\':float(x.get(\'total_pnl\') or 0.0),\n                    \'profit_factor\':gp/gl if gl>1e-9 else (999.0 if gp>0 else None),\n                    \'payoff_ratio\':aw/al if al>1e-9 else (999.0 if aw>0 else None)}\n        base=norm(by.get(\'veritas-portfolio-v6-v80-unified-execution\'))\n        cur=norm(by.get(\'veritas-portfolio-v8.2-v84-audited-execution\'))\n        measurable=cur[\'n\']>=20 and base[\'n\']>=8\n        gate=None\n        if measurable:\n            pf_ok=(base[\'profit_factor\'] is None or\n                   (cur[\'profit_factor\'] is not None and cur[\'profit_factor\']>=base[\'profit_factor\']))\n            gate=bool(cur[\'win_rate\'] is not None and base[\'win_rate\'] is not None\n                      and cur[\'win_rate\']>=base[\'win_rate\']\n                      and cur[\'avg_pnl\']>=base[\'avg_pnl\']\n                      and pf_ok)\n        return {\'status\':\'MEASURABLE\' if measurable else \'BUILDING\',\n                \'frozen_baseline\':\'veritas-portfolio-v6-v80-unified-execution\',\n                \'baseline\':base,\'v84_2\':cur,\'quality_gate_pass\':gate,\n                \'promotion_rule\':\'win rate must improve or hold; average P&L and profit factor may not deteriorate\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'error\':f\'{type(ex).__name__}: {ex}\'}\n'
PORTFOLIO_V84_HELPER="\ndef _candidate_book_v84(summary):\n    # Unified multi-timeframe portfolio routing with experience-aware ranking.\n    grouped={}\n    for r0 in summary or []:\n        r=dict(r0)\n        d=str(r.get('research_decision') or 'NO_TRADE')\n        tr=r.get('tactical_reversal') or {}\n        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'):\n            d=str(tr.get('direction')); r['research_decision']=d\n        if d not in ('LONG','SHORT'): continue\n        if not bool(r.get('source_gate_pass',True)) or not bool(r.get('market_open',True)): continue\n        p,source=_signal_probability(r)\n        inst=r.get('institutional_signal') or {}; plan=r.get('trade_plan') or {}\n        ev=inst.get('evidence_independence') or {}; bq=inst.get('breakout_quality') or {}\n        indep=int(ev.get('independent_count') or 0); q=float(bq.get('quality_score') or 0.0)\n        rr=float(plan.get('expected_to_stop_ratio') or 0.0); tq=float(plan.get('trade_quality_score') or 0.0)\n        xp=plan.get('setup_memory') or {}; xpp=xp.get('posterior_win_rate')\n        bonus=0.0\n        if xpp is not None and float(xp.get('effective_n') or 0)>=8:\n            bonus=_clip((float(xpp)-0.50)*0.12,-0.03,0.03)\n        rank=float(p)+0.02*min(indep,6)+0.03*q+0.02*min(max(rr,0.0),2.0)+0.03*tq+bonus\n        r['_pwin']=p; r['_pwin_source']=source; r['_rank']=rank; r['_signal_first']=True\n        grouped.setdefault(str(r.get('asset')),[]).append(r)\n\n    out={}\n    for asset,rows in grouped.items():\n        support={'LONG':0.0,'SHORT':0.0}; hs={'LONG':[],'SHORT':[]}\n        for r in rows:\n            d=str(r.get('research_decision'))\n            support[d]+=max(0.01,float(r.get('_rank') or 0.0))\n            hs[d].append(str(r.get('horizon')))\n        chosen='LONG' if support['LONG']>=support['SHORT'] else 'SHORT'\n        eligible=[r for r in rows if str(r.get('research_decision'))==chosen]\n        if not eligible: continue\n        best=max(eligible,key=lambda r:float(r.get('_rank') or 0.0))\n        best=dict(best)\n        best['_direction_support']=support\n        best['_supporting_horizons']=sorted(set(hs[chosen]))\n        other='SHORT' if chosen=='LONG' else 'LONG'\n        ratio=float(support[chosen])/max(0.01,float(support[other]))\n        piv=best.get('impulse_pivot_break') or {}; rev=best.get('tactical_reversal') or {}\n        fast_confirm=bool(\n            (piv.get('active') and float(piv.get('probability') or 0)>=0.76) or\n            (rev.get('active') and float(rev.get('probability') or 0)>=0.78)\n        )\n        best['_flip_confirmed']=bool(\n            fast_confirm or (len(best['_supporting_horizons'])>=2 and ratio>=1.20)\n        )\n        out[asset]=best\n    return out\n\n\n# =========================\n# VERITAS v84.2 PORTFOLIO AUDITED OVERRIDES\n# =========================\n\ndef _v842_position_payload(z):\n    p=(z or {}).get('payload') or {}\n    if isinstance(p,dict): return p\n    try: return json.loads(p)\n    except Exception: return {}\n\ndef _v842_management_row(summary,z):\n    asset=str((z or {}).get('asset') or '')\n    payload=_v842_position_payload(dict(z) if z is not None else {})\n    eh=str(payload.get('execution_horizon') or payload.get('horizon') or '')\n    rows=[r for r in (summary or []) if str(r.get('asset') or '')==asset]\n    if eh:\n        exact=[r for r in rows if str(r.get('horizon') or '')==eh]\n        if exact:\n            return max(exact,key=lambda r:float(r.get('confidence') or 0.0))\n    hard=[r for r in rows if bool(((r.get('trade_plan') or {}).get('trade_integrity') or {}).get('hard_invalidation'))]\n    if hard:\n        return max(hard,key=lambda r:float(r.get('confidence') or 0.0))\n    return max(rows,key=lambda r:float(r.get('confidence') or 0.0)) if rows else None\n\ndef _v842_hard_thesis_exit(row):\n    if not row: return False\n    plan=row.get('trade_plan') or {}\n    ti=plan.get('trade_integrity') or {}\n    return bool(ti.get('hard_invalidation'))\n\ndef _signal_first_admission(row,policy,drawdown):\n    if not row:\n        return {'open':False,'fraction':0.0,'reason':'NO_ROW'}\n    d=str(row.get('research_decision') or 'NO_TRADE')\n    if d not in ('LONG','SHORT'):\n        return {'open':False,'fraction':0.0,'reason':'NO_DIRECTION'}\n    if not bool(row.get('source_gate_pass',True)):\n        return {'open':False,'fraction':0.0,'reason':'SOURCE_GATE'}\n    if not bool(row.get('market_open',True)):\n        return {'open':False,'fraction':0.0,'reason':'MARKET_CLOSED'}\n\n    plan=row.get('trade_plan') or {}\n    ti=plan.get('trade_integrity') or {}\n    ec=plan.get('execution_consistency') or {}\n    arb=plan.get('rule_arbitration') or {}\n    hard=bool(\n        ti.get('hard_invalidation')\n        or ec.get('status')=='VETO'\n        or ((arb.get('hard_veto') or {}).get('decision')=='VETO')\n        or (plan.get('reentry_intelligence') or {}).get('allowed') is False\n    )\n    if hard:\n        return {'open':False,'fraction':0.0,'reason':'HARD_VETO'}\n\n    mode=str(policy.get('mode') or 'CORE')\n    base=0.10 if mode=='CORE' else 0.05\n    p=float(row.get('_pwin') or 0.50)\n    source=str(row.get('_pwin_source') or '')\n    inst=row.get('institutional_signal') or {}\n    indep=int(((inst.get('evidence_independence') or {}).get('independent_count')) or 0)\n    rr=float(plan.get('expected_to_stop_ratio') or 0.0)\n    tq=float(plan.get('trade_quality_score') or 0.0)\n    action=str(inst.get('action') or '')\n    shift=str(plan.get('regime_shift_state') or '')\n    mem=plan.get('setup_memory') or {}\n    memory_ready=str(mem.get('status') or '') in ('EXECUTION_READY','WEIGHT_READY')\n    empirical=(source=='EMPIRICAL_CALIBRATION')\n\n    # Uncalibrated pwin may justify a probe, not a large position.\n    f=base\n    if p>=0.65: f=max(f,0.15)\n    if empirical or memory_ready:\n        if p>=0.72 and indep>=2: f=max(f,0.25)\n        if p>=0.78 and indep>=3 and rr>=1.0: f=max(f,0.35)\n        if p>=0.82 and indep>=4 and rr>=1.2: f=max(f,0.50)\n        if action in ('ENTER_AND_SCALE','ENTER_FULL_CANDIDATE') and p>=0.82 and rr>=1.2:\n            f=max(f,0.50)\n    elif p>=0.75 and indep>=3 and rr>=1.0 and tq>=0.50:\n        f=max(f,0.20)\n\n    # Decision layer provides one authoritative v84 target. It is a cap until\n    # confirmation is statistically mature; it is never multiplied again here.\n    planned=float(plan.get('v84_target_fraction') or 0.0)\n    ep=plan.get('execution_policy') or {}\n    entry_mode=str(ep.get('entry_mode') or '')\n    if planned>0:\n        if entry_mode=='CONFIRMED_SCALE' and (empirical or memory_ready):\n            f=max(f,min(planned,0.50))\n        else:\n            f=min(f,max(0.05,planned))\n\n    if ti.get('entry_permission')=='WAIT_ENTRY':\n        f=min(f,0.05)\n    if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):\n        f=min(f,0.10)\n    if tq>0 and tq<0.45:\n        f=min(f,0.05)\n    if rr>0 and rr<0.60:\n        f=min(f,0.05)\n\n    # Use the post-v84 structural stop distance first; institutional risk_pct may be stale.\n    risk_pct=plan.get('stop_distance_pct')\n    if risk_pct is None:\n        risk_pct=inst.get('risk_pct')\n    if risk_pct is not None:\n        try:\n            rp=float(risk_pct)\n            if rp>0:\n                f=min(f,MAX_STOP_RISK_NAV/rp)\n        except Exception:\n            pass\n\n    rg=_risk_governor(drawdown)\n    if rg.get('new_risk') is False:\n        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD',\n                'risk_governor':rg}\n    f=max(0.05,f)\n    f*=float(rg.get('multiplier') or 0.0)\n    maxf=float(policy.get('max_fraction') or 2.0)\n    if 0<f<POSITION_STEP:\n        f=min(f,maxf)\n    else:\n        f=_clip(_round_step(f),0,maxf)\n    return {'open':f>0,'fraction':f,'reason':'SIGNAL_FIRST_V842',\n            'pwin':p,'pwin_source':source,'empirical':empirical,\n            'memory_ready':memory_ready,'independent':indep,'rr':rr,\n            'trade_quality':tq,'entry_permission':ti.get('entry_permission'),\n            'planned_fraction':planned,'risk_governor':rg,\n            'sizing_authority':'DECISION_TARGET_THEN_PORTFOLIO_RISK'}\n\n"


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write(path,text):
    path.write_text(text,encoding="utf-8")


def _already_v80():
    intel=_read(TARGET); port=_read(PORTFOLIO_TARGET)
    # A failed v84 overlay may already have upgraded one module. Treat that as
    # a valid v80 foundation so the next startup can resume idempotently.
    return (V80_INTEL in intel or V84_INTEL in intel) and (V80_PORT in port or V84_PORT in port)


def _already_v84():
    return V84_INTEL in _read(TARGET) and V84_PORT in _read(PORTFOLIO_TARGET)


def _ensure_hashlib():
    if not PORTFOLIO_TARGET.exists():
        raise RuntimeError("veritas_portfolio.py missing")
    src=_read(PORTFOLIO_TARGET)
    if "hashlib.sha256" not in src:
        return False
    head="\n".join(src.splitlines()[:30])
    if "import hashlib" in head or "hashlib," in head or ", hashlib" in head:
        return False
    anchor="from __future__ import annotations\n"
    dst=src.replace(anchor,anchor+"import hashlib\n",1) if anchor in src else "import hashlib\n"+src
    _write(PORTFOLIO_TARGET,dst)
    return True


def _load_v80():
    req=Request(PINNED_V80_URL,headers={"User-Agent":"VERITAS-v84-bootstrap/1.0"})
    with urlopen(req,timeout=30) as r:
        return r.read().decode("utf-8")


def _fix_v80_order(src):
    s=src.find(START_MARKER); e=src.find(END_MARKER)
    if s<0 or e<0 or e<=s:
        raise RuntimeError("v78.1 markers missing in pinned v80")
    block=src[s:e]
    src=src[:s]+src[e:]
    ins=src.find(INSERT_MARKER)
    if ins<0:
        raise RuntimeError("v79 marker missing in pinned v80")
    fixed=src[:ins]+block+"\n"+src[ins:]
    pp=fixed.find("PORTFOLIO_PATCHES = (")
    if not (0<=fixed.find(START_MARKER)<fixed.find(INSERT_MARKER)<pp):
        raise RuntimeError("v80 patch order repair failed")
    if START_MARKER in fixed[pp:]:
        raise RuntimeError("v78.1 remained in portfolio patches")
    return fixed


def _execute_v80():
    payload=_fix_v80_order(_load_v80())
    glb={"__name__":"__veritas_v80_for_v84__","__file__":str(Path(__file__).resolve()),"__package__":None}
    exec(compile(payload,str(Path(__file__).resolve()),"exec"),glb,glb)


def _replace_once(src,old,new,label):
    if new in src:
        return src,False
    if old not in src:
        raise RuntimeError("v84 expected pattern not found: "+label)
    return src.replace(old,new,1),True


def _insert_before_once(src,anchor,block,marker,label):
    if marker in src:
        return src,False
    if anchor not in src:
        raise RuntimeError("v84 insertion anchor not found: "+label)
    return src.replace(anchor,block+"\n\n"+anchor,1),True


def _patch_intelligence():
    src=_read(TARGET); dst=src; applied=[]

    if V84_INTEL not in dst:
        if "VERSION = 'veritas-max-product-v80.0-unified-execution-core'" in dst:
            dst=dst.replace(
                "VERSION = 'veritas-max-product-v80.0-unified-execution-core'",
                "VERSION = 'veritas-max-product-v84.2-audited-learning-execution'",1)
        elif 'VERSION = "veritas-max-product-v80.0-unified-execution-core"' in dst:
            dst=dst.replace(
                'VERSION = "veritas-max-product-v80.0-unified-execution-core"',
                'VERSION = "veritas-max-product-v84.2-audited-learning-execution"',1)
        else:
            raise RuntimeError("v84 intelligence version anchor missing")
        applied.append("version")

    dst,ch=_insert_before_once(
        dst,
        "def trade_decision_stage(direction,trade_plan,tradeability,structure=None):",
        V84_HELPER_BLOCK,
        "# VERITAS v81-v84 ADAPTIVE EXPERIENCE EXECUTION",
        "v81-v84 helper core")
    if ch: applied.append("experience_setup_regime_execution_core")

    old="                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])\n                agents = agent_views(f, horizon, deriv, asset)\n                research_dec, conf, size, score, used_weights, impulse_overlay = committee(\n                    agents, asset, horizon, perf, f['regime'], knowledge_adjustment.get('score',0.0))\n                research_challenger=challenger_committee(\n                    agents,asset,horizon,perf,f['regime'],knowledge_adjustment.get('score',0.0))"
    new="                knowledge_adjustment = dict(validated_knowledge_adjustment(kmatches,asset,horizon,f['regime']))\n                experience_prior=experience_direction_prior(asset,horizon,f)\n                base_knowledge_score=float(knowledge_adjustment.get('score',0.0) or 0.0)\n                experience_prior_score=float(experience_prior.get('score_adjustment') or 0.0)\n                combined_adjustment=clip(base_knowledge_score+experience_prior_score,-0.05,0.05)\n                knowledge_adjustment['base_score']=base_knowledge_score\n                knowledge_adjustment['experience_prior']=experience_prior\n                knowledge_adjustment['score_with_experience']=combined_adjustment\n                agents = agent_views(f, horizon, deriv, asset)\n                research_dec, conf, size, score, used_weights, impulse_overlay = committee(\n                    agents, asset, horizon, perf, f['regime'], combined_adjustment)\n                research_challenger=challenger_committee(\n                    agents,asset,horizon,perf,f['regime'],combined_adjustment)"
    dst,ch=_replace_once(dst,old,new,"v84.2 bounded experience direction prior")
    if ch: applied.append("experience_direction_prior")

    old="""                tradeability=tradeability_analog_stats(asset,horizon,f,research_dec)
                decision_stage=trade_decision_stage(research_dec,trade_plan,tradeability,f.get('intraday_structure') or {})
                trade_plan['tradeability']=tradeability
                trade_plan['decision_stage']=decision_stage
                trade_plan['positive_trade_probability']=tradeability.get('positive_trade_probability')
                trade_plan['statistical_noise_buffer_p80']=tradeability.get('p80_adverse_excursion')"""
    new="""                tradeability=tradeability_analog_stats(asset,horizon,f,research_dec)
                v84_row=dict(f); v84_row.update({
                    'asset':asset,'horizon':horizon,'research_decision':research_dec,
                    'trade_plan':trade_plan,'institutional_signal':institutional_signal,
                    'tactical_reversal':tactical_reversal,'impulse_pivot_break':f.get('impulse_pivot_break') or {},
                    'range_retest_breakout':f.get('range_retest_breakout') or {},
                    'horizon_structure':f.get('horizon_structure') or {}})
                experience_decision=experience_profile_for_trade(asset,horizon,v84_row,research_dec,trade_plan,tradeability)
                setup_memory=experience_decision.get('setup_memory') or setup_memory_profile(asset,horizon,v84_row,research_dec,trade_plan)
                regime_policy=adaptive_regime_policy(asset,horizon,v84_row,research_dec,trade_plan,setup_memory)
                execution_policy=execution_policy_v84(asset,horizon,v84_row,research_dec,trade_plan,setup_memory,regime_policy)
                trade_plan=apply_v84_execution_to_trade_plan(v84_row,trade_plan,setup_memory,regime_policy,execution_policy)
                trade_plan['experience_decision']=experience_decision
                decision_stage=trade_decision_stage(research_dec,trade_plan,tradeability,f.get('intraday_structure') or {})
                trade_plan['tradeability']=tradeability
                trade_plan['decision_stage']=decision_stage
                trade_plan['positive_trade_probability']=tradeability.get('positive_trade_probability')
                trade_plan['statistical_noise_buffer_p80']=tradeability.get('p80_adverse_excursion')
                f['experience_decision']=experience_decision
                f['setup_memory']=setup_memory
                f['adaptive_regime_policy']=regime_policy
                f['execution_policy']=execution_policy"""
    dst,ch=_replace_once(dst,old,new,"v82-v84 trade integration")
    if ch: applied.append("setup_regime_execution")

    old="""    if d in ('LONG','SHORT') and d!=current_direction:
        return 'EXIT','confirmed_dominant_direction_flip'
    return None,None"""
    new="""    if d in ('LONG','SHORT') and d!=current_direction:
        if v84_direction_flip_confirmed(x,current_direction):
            return 'EXIT','v84_confirmed_multi_horizon_direction_flip'
        return None,'v84_soft_direction_flip_ignored'
    return None,None"""
    dst,ch=_replace_once(dst,old,new,"v84 direction flip guard")
    if ch: applied.append("exit_flip_guard")

    old="""                 'stop_method':plan.get('stop_method'),'setup_family':_uec_setup_family(x),
                 'structural_anchor':_uec_anchor(x,d),'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}"""
    new="""                 'stop_method':plan.get('stop_method'),'setup_family':_uec_setup_family(x),
                 'experience_decision':plan.get('experience_decision'),
                 'setup_memory':plan.get('setup_memory'),
                 'adaptive_regime_policy':plan.get('adaptive_regime_policy'),
                 'execution_policy':plan.get('execution_policy'),
                 'structural_anchor':_uec_anchor(x,d),'robot_eligible':False,'execution_mode':'SHADOW_ONLY'}"""
    dst,ch=_replace_once(dst,old,new,"persist v84 canonical policy")
    if ch: applied.append("canonical_policy")

    old="""                stage=str(x.get('decision_stage') or tr.get('stage') or 'HOLD'); plan=x.get('trade_plan') or {}; ta=x.get('tradeability') or {}
                desired=clip(float(plan.get('initial_position_fraction') or cur),0.0,1.0)
                # Confirmation can add; deteriorating evidence can reduce, but only in shadow until separately validated.
                if stage in ('CONFIRMED_SCALE','CONFIRMED_FULL') and desired>cur+1e-6:"""
    new="""                plan=x.get('trade_plan') or {}
                execp=plan.get('execution_policy') or setup_payload.get('execution_policy') or {}
                stage=str(x.get('decision_stage') or tr.get('stage') or 'HOLD'); ta=x.get('tradeability') or {}
                desired=clip(float(plan.get('initial_position_fraction') or cur),0.05,1.0)
                if execp and not bool(execp.get('add_allowed',True)):
                    desired=min(desired,cur)

                # Scale only after confirmation and only when v84 regime/setup memory permits it.
                if desired>cur+1e-6 and stage in ('CONFIRMED_SCALE','CONFIRMED_FULL','ENTER_AND_SCALE','ENTER_FULL_CANDIDATE'):"""
    dst,ch=_replace_once(dst,old,new,"v84 shadow add policy")
    if ch: applied.append("entry_scale_policy")

    old="""                payload={'canonical_trade_state':True,'execution_horizon':sp.get('execution_horizon') or st.get('horizon'),
                         'supporting_horizons':sp.get('supporting_horizons') or [],
                         'regime_open':x.get('regime'),'signal_tier_open':x.get('signal_tier'),
                         'execution_mode':'SHADOW_ONLY','path_dependent':True}"""
    new="""                payload={'canonical_trade_state':True,'execution_horizon':sp.get('execution_horizon') or st.get('horizon'),
                         'supporting_horizons':sp.get('supporting_horizons') or [],
                         'regime_open':x.get('regime'),'signal_tier_open':x.get('signal_tier'),
                         'initial_stop_price':st.get('stop_price'),
                         'experience_decision_open':sp.get('experience_decision'),
                         'setup_memory_open':sp.get('setup_memory'),
                         'adaptive_regime_policy_open':sp.get('adaptive_regime_policy'),
                         'execution_policy_open':sp.get('execution_policy'),
                         'execution_mode':'SHADOW_ONLY','path_dependent':True}"""
    dst,ch=_replace_once(dst,old,new,"shadow policy persistence")
    if ch: applied.append("shadow_policy")

    old="""        rr=refresh_rule_stats()
        rule_seconds=time.time()-t
        dur=time.time()-started"""
    new="""        rr=refresh_rule_stats()
        rule_seconds=time.time()-t
        t=time.time()
        xp=refresh_experience_lessons()
        experience_lessons_seconds=time.time()-t
        t=time.time()
        new_xp=int(xp.get('trade_lessons') or 0)+int(xp.get('rejected_lessons') or 0)+int(xp.get('abstention_lessons') or 0) if isinstance(xp,dict) else 0
        memory_cache=getattr(setup_memory_board,'_cache',None)
        if new_xp>0 or not memory_cache:
            memory_snapshot=setup_memory_board(force=True)
        else:
            memory_snapshot=setup_memory_board(force=False)
        experience_memory_seconds=time.time()-t
        experience_seconds=experience_lessons_seconds+experience_memory_seconds
        dur=time.time()-started"""
    dst,ch=_replace_once(dst,old,new,"background experience learning")
    if ch: applied.append("background_learning")

    old="""                'event_learning':ev,'rule_learning':rr,'last_error':None,
                'runs':int(heavy_learning_state.get('runs') or 0)+1,
                'event_seconds':round(event_seconds,3),'rule_seconds':round(rule_seconds,3),'reason':reason"""
    new="""                'event_learning':ev,'rule_learning':rr,'experience_learning':xp,'last_error':None,
                'runs':int(heavy_learning_state.get('runs') or 0)+1,
                'event_seconds':round(event_seconds,3),'rule_seconds':round(rule_seconds,3),
                'experience_seconds':round(experience_seconds,3),
                'experience_lessons_seconds':round(experience_lessons_seconds,3),
                'experience_memory_seconds':round(experience_memory_seconds,3),
                'experience_memory_status':memory_snapshot.get('status') if isinstance(memory_snapshot,dict) else None,
                'experience_memory_items':len(memory_snapshot.get('items') or []) if isinstance(memory_snapshot,dict) else 0,
                'reason':reason"""
    dst,ch=_replace_once(dst,old,new,"background learning state")
    if ch: applied.append("learning_state")

    old="""             event_seconds=round(event_seconds,3),rule_seconds=round(rule_seconds,3),
             event_written=ev.get('written') if isinstance(ev,dict) else None,
             rule_rows=rr.get('rows') if isinstance(rr,dict) else None)"""
    new="""             event_seconds=round(event_seconds,3),rule_seconds=round(rule_seconds,3),
             experience_seconds=round(experience_seconds,3),
             experience_lessons_seconds=round(experience_lessons_seconds,3),
             experience_memory_seconds=round(experience_memory_seconds,3),
             experience_memory_items=len(memory_snapshot.get('items') or []) if isinstance(memory_snapshot,dict) else 0,
             event_written=ev.get('written') if isinstance(ev,dict) else None,
             rule_rows=rr.get('rows') if isinstance(rr,dict) else None,
             experience_trade_lessons=xp.get('trade_lessons') if isinstance(xp,dict) else None,
             experience_rejected_lessons=xp.get('rejected_lessons') if isinstance(xp,dict) else None,
             experience_abstention_lessons=xp.get('abstention_lessons') if isinstance(xp,dict) else None)"""
    dst,ch=_replace_once(dst,old,new,"experience telemetry")
    if ch: applied.append("learning_telemetry")

    old="""        'factory':knowledge_factory_status(),'research_discovery_health':research_discovery_health(),
        'production_readiness':"""
    new="""        'factory':knowledge_factory_status(),'research_discovery_health':research_discovery_health(),
        'experience_learning':execution_learning_board(),'learning_index_v2':learning_index_v2(),
        'production_readiness':"""
    dst,ch=_replace_once(dst,old,new,"overview v84 learning")
    if ch: applied.append("overview")

    # v84.3 operational repair: bound learning analytics to indexed slices.
    # Full-history window CTEs saturated the 0.1 CPU PostgreSQL and caused DB restarts.
    bounded_learning = r'''
# =========================
# VERITAS v84.3 BOUNDED LEARNING SQL
# Keeps learning durable while preventing analytical full-table scans from blocking
# the web/portfolio fast path on small PostgreSQL instances.
# =========================

def _bounded_completed_episode_rows(order='DESC', raw_limit=9000, episode_limit=600):
    if not pg_enabled():
        return []
    order='ASC' if str(order).upper()=='ASC' else 'DESC'
    raw_limit=max(1000,min(20000,int(raw_limit)))
    episode_limit=max(50,min(4000,int(episode_limit)))
    sql=f"""
      WITH picked AS (
        SELECT entity_key,event_ts,asset,horizon,payload,model_version
        FROM ledger_events
        WHERE event_type='decision'
        ORDER BY event_ts {order}
        LIMIT %s
      )
      SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload AS dp,d.model_version,
             o.payload AS op
      FROM picked d
      JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type='outcome'
      WHERE o.payload ? 'forward_return'
      ORDER BY d.event_ts {order}
    """
    try:
        with pg_connect() as c:
            c.execute("SET statement_timeout TO '12s'")
            rows=[dict(r) for r in c.execute(sql,(raw_limit,)).fetchall()]
    except Exception as ex:
        emit('bounded_learning_query_error',order=order,raw_limit=raw_limit,
             error=f'{type(ex).__name__}: {ex}')
        return []

    # Episode detection must run chronologically. For a recent DESC slice, reverse first,
    # then retain the newest independent episodes.
    if order=='DESC':
        rows=list(reversed(rows))
    gaps={'1h':1800,'4h':7200,'1d':21600,'3d':43200,'7d':86400}
    last={}
    episodes=[]
    for r in rows:
        dp=r.get('dp') if isinstance(r.get('dp'),dict) else _v84_json(r.get('dp'))
        op=r.get('op') if isinstance(r.get('op'),dict) else _v84_json(r.get('op'))
        fr=op.get('forward_return')
        if fr is None:
            continue
        dec=str(dp.get('research_decision') or dp.get('decision') or 'NO_TRADE')
        regime=str(dp.get('regime') or 'UNKNOWN')
        ts=r.get('event_ts')
        if isinstance(ts,str):
            try: ts=datetime.fromisoformat(ts.replace('Z','+00:00'))
            except Exception: ts=None
        if ts is not None and getattr(ts,'tzinfo',None) is None:
            ts=ts.replace(tzinfo=timezone.utc)
        key=(str(r.get('asset')),str(r.get('horizon')))
        prev=last.get(key)
        is_new=(prev is None or dec!=prev['decision'] or regime!=prev['regime'])
        if not is_new and ts is not None and prev.get('ts') is not None:
            is_new=(ts-prev['ts']).total_seconds()>gaps.get(key[1],86400)
        last[key]={'decision':dec,'regime':regime,'ts':ts}
        if not is_new:
            continue
        episodes.append({
            'entity_key':r.get('entity_key'),'event_ts':r.get('event_ts'),
            'asset':r.get('asset'),'horizon':r.get('horizon'),'regime':regime,
            'decision':dec,'research_decision':dec,'forward_return':float(fr),
            'model_version':r.get('model_version'),'dp':dp,'op':op
        })
    return episodes[-episode_limit:] if order=='DESC' else episodes[:episode_limit]


def _matched_strata_learning():
    if not pg_enabled():
        return {'status':'postgres_required'}
    fetch=max(200,min(600,LEARNING_PROGRESS_WINDOW*5))
    raw=max(5000,min(12000,fetch*16))
    early=_bounded_completed_episode_rows('ASC',raw,fetch)
    recent=_bounded_completed_episode_rows('DESC',raw,fetch)
    eg={}; rg={}
    for r in early:
        eg.setdefault((r['asset'],r['horizon'],str(r.get('regime') or 'UNKNOWN')),[]).append(r)
    for r in recent:
        rg.setdefault((r['asset'],r['horizon'],str(r.get('regime') or 'UNKNOWN')),[]).append(r)
    pairs=[]
    for k in sorted(set(eg)&set(rg)):
        n=min(len(eg[k]),len(rg[k]),LEARNING_INDEX_MAX_PER_STRATUM)
        if n<LEARNING_INDEX_STRATA_MIN_N:
            continue
        e=_learning_metrics_extended(eg[k][:n])
        r=_learning_metrics_extended(rg[k][-n:])
        pairs.append((k,n,e,r))
    def avg(field,which):
        vals=[]
        for _,_,e,r in pairs:
            v=(e if which=='e' else r).get(field)
            if v is not None:
                vals.append(float(v))
        return sum(vals)/len(vals) if vals else None
    em={x:avg(x,'e') for x in ('hit_rate','avg_signed_return','no_trade_miss_rate','capture_rate','wrong_side_rate')}
    rm={x:avg(x,'r') for x in ('hit_rate','avg_signed_return','no_trade_miss_rate','capture_rate','wrong_side_rate')}
    em['n']=sum(n for _,n,_,_ in pairs); rm['n']=em['n']
    return {
        'status':'ok' if pairs else 'BUILDING','baseline':em,'current':rm,
        'matched_strata':len(pairs),'matched_observations_each_side':em['n'],
        'strata':[{'asset':k[0],'horizon':k[1],'regime':k[2],'n_each':n} for k,n,_,_ in pairs[:80]],
        'sampling':'bounded_indexed_episode_slices','raw_limit_each_side':raw,'episode_limit_each_side':fetch
    }


def learning_progress_v1():
    if not pg_enabled():
        return {'status':'postgres_required'}
    lim=max(30,min(300,LEARNING_PROGRESS_WINDOW))
    raw=max(3000,min(8000,lim*24))
    early=_bounded_completed_episode_rows('ASC',raw,lim)
    recent=_bounded_completed_episode_rows('DESC',raw,lim)
    em=_window_learning_metrics(early)
    rm=_window_learning_metrics(recent)
    if em['n']<20 or rm['n']<20 or em.get('hit_rate') is None or rm.get('hit_rate') is None:
        idx=None; status='BUILDING'
    else:
        hit_component=clip(rm['hit_rate']/max(em['hit_rate'],0.20),0.5,1.5)
        bmiss=em.get('no_trade_miss_rate'); rmiss=rm.get('no_trade_miss_rate')
        miss_component=1.0 if bmiss is None or rmiss is None else clip((1-rmiss)/max(0.2,1-bmiss),0.5,1.5)
        be=em.get('avg_signed_return') or 0.0; re=rm.get('avg_signed_return') or 0.0
        edge_component=clip(1.0+(re-be)/0.01,0.5,1.5)
        idx=round(100*(0.55*hit_component+0.25*miss_component+0.20*edge_component),1)
        status='MEASURABLE'
    try:
        with pg_connect() as c:
            c.execute("SET statement_timeout TO '5s'")
            kg=c.execute("SELECT COUNT(*) sources FROM knowledge_sources").fetchone()
            kr=c.execute("SELECT COUNT(*) rules FROM knowledge_rules").fetchone()
    except Exception:
        kg=kr={}
    versions=[]
    for r in early+recent:
        if r.get('model_version') and r['model_version'] not in versions:
            versions.append(r['model_version'])
    confidence='HIGH' if min(em['n'],rm['n'])>=100 else 'MEDIUM' if min(em['n'],rm['n'])>=40 else 'LOW'
    return {
        'status':status,'index_vs_start':idx,'baseline_index':100,'confidence':confidence,'window':lim,
        'baseline':em,'current':rm,
        'hit_rate_delta_pp':None if em.get('hit_rate') is None or rm.get('hit_rate') is None else round(100*(rm['hit_rate']-em['hit_rate']),2),
        'avg_signed_return_delta':None if em.get('avg_signed_return') is None or rm.get('avg_signed_return') is None else rm['avg_signed_return']-em['avg_signed_return'],
        'no_trade_miss_delta_pp':None if em.get('no_trade_miss_rate') is None or rm.get('no_trade_miss_rate') is None else round(100*(rm['no_trade_miss_rate']-em['no_trade_miss_rate']),2),
        'knowledge_growth':{'current_sources':(kg or {}).get('sources'),'current_rules':(kr or {}).get('rules')},
        'versions_seen':versions[-8:],
        'sampling':'bounded_indexed_episode_slices',
        'definition':'100 = earliest bounded independent completed-decision window; higher is better only when sample is measurable.'
    }


def refresh_rule_stats():
    # Recent independent evidence is sufficient for live lifecycle governance; OOS statistics
    # remain the promotion authority. Avoid recomputing window functions over the full ledger.
    if not pg_enabled():
        return {'rows':0,'status_changes':0,'status':'postgres_required'}
    episodes=_bounded_completed_episode_rows('DESC',12000,3500)
    if not episodes:
        return {'rows':0,'status_changes':0,'status':'bounded_query_empty'}
    buckets={}
    for r in episodes:
        dp=r.get('dp') or {}; op=r.get('op') or {}
        try: fr=float(op.get('forward_return'))
        except Exception: continue
        mfe=op.get('mfe'); mae=op.get('mae')
        try: mfe=None if mfe is None else float(mfe)
        except Exception: mfe=None
        try: mae=None if mae is None else float(mae)
        except Exception: mae=None
        for k in (dp.get('knowledge_shadow_matches') or []):
            if not isinstance(k,dict):
                continue
            action=str(k.get('action') or '')
            rid=k.get('rule_id')
            if not rid or action not in ('LONG','SHORT'):
                continue
            key=(str(rid),str(r.get('asset')),str(r.get('horizon')))
            z=buckets.setdefault(key,{'n':0,'hits':0,'ret':0.0,'mfe':0.0,'mfe_n':0,'mae':0.0,'mae_n':0})
            sr=fr if action=='LONG' else -fr
            z['n']+=1; z['hits']+=1 if sr>0 else 0; z['ret']+=sr
            smfe=mfe if action=='LONG' else (None if mae is None else -mae)
            smae=mae if action=='LONG' else (None if mfe is None else -mfe)
            if smfe is not None: z['mfe']+=smfe; z['mfe_n']+=1
            if smae is not None: z['mae']+=smae; z['mae_n']+=1
    try:
        with pg_connect() as c:
            c.execute("SET statement_timeout TO '12s'")
            for (rid,asset,horizon),z in buckets.items():
                n=z['n']; hits=z['hits']; hr=hits/n if n else None
                c.execute("""INSERT INTO knowledge_rule_stats(rule_id,asset,horizon,n,hits,hit_rate,avg_signed_return,avg_mfe,avg_mae,updated_at)
                             VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                             ON CONFLICT(rule_id,asset,horizon) DO UPDATE SET
                             n=EXCLUDED.n,hits=EXCLUDED.hits,hit_rate=EXCLUDED.hit_rate,
                             avg_signed_return=EXCLUDED.avg_signed_return,avg_mfe=EXCLUDED.avg_mfe,
                             avg_mae=EXCLUDED.avg_mae,updated_at=EXCLUDED.updated_at""",
                          (rid,asset,horizon,n,hits,hr,z['ret']/n if n else None,
                           z['mfe']/z['mfe_n'] if z['mfe_n'] else None,
                           z['mae']/z['mae_n'] if z['mae_n'] else None,now()))
        changes=apply_rule_lifecycle()
        return {'rows':len(buckets),'status_changes':changes,'status':'bounded_recent_episode_window',
                'episodes_used':len(episodes)}
    except Exception as ex:
        emit('bounded_rule_stats_error',error=f'{type(ex).__name__}: {ex}')
        return {'rows':0,'status_changes':0,'status':'error','error':f'{type(ex).__name__}: {ex}'}
'''
    dst,ch=_insert_before_once(
        dst,"def _learning_progress_v2_compute():",bounded_learning,
        "def _bounded_completed_episode_rows(","v84.3 bounded learning SQL")
    if ch: applied.append("bounded_learning_sql")

    if dst!=src:
        _write(TARGET,dst)
    return applied


def _patch_portfolio():
    src=_read(PORTFOLIO_TARGET); dst=src; applied=[]

    if V84_PORT not in dst:
        if "VERSION='veritas-portfolio-v6-v80-unified-execution'" not in dst:
            raise RuntimeError("v84 portfolio version anchor missing")
        dst=dst.replace(
            "VERSION='veritas-portfolio-v6-v80-unified-execution'",
            "VERSION='veritas-portfolio-v8.2-v84-audited-execution'",1)
        applied.append("version")

    dst,ch=_insert_before_once(
        dst,"def _risk_governor(drawdown):",PORTFOLIO_V84_HELPER,
        "def _candidate_book_v84(summary):","v84 portfolio candidate helper")
    if ch: applied.append("unified_candidate_book")

    old="""    rg=_risk_governor(drawdown)
    f*=rg['multiplier']
    maxf=float(policy.get('max_fraction') or 2.0)
    f=_clip(_round_step(f),0,maxf)
    return {'open':f>0,'fraction':f,'reason':'SIGNAL_FIRST',"""
    new="""    xp=plan.get('execution_policy') or {}
    if xp.get('decision_influence'):
        f*= _clip(float(xp.get('size_multiplier') or 1.0),0.50,1.30)
    rg=_risk_governor(drawdown)
    if rg.get('new_risk') is False:
        return {'open':False,'fraction':0.0,'reason':'RISK_GOVERNOR_HARD','experience_decision':xp}
    f*=rg['multiplier']
    # v84 signal-first invariant: soft learning can reduce to probe, never erase a valid signal.
    f=max(0.05,f)
    maxf=float(policy.get('max_fraction') or 2.0)
    f=_clip(_round_step(f),0.05,maxf)
    return {'open':f>0,'fraction':f,'reason':'SIGNAL_FIRST',
            'experience_decision':xp,"""
    dst,ch=_replace_once(dst,old,new,"v84 experience sizing")
    if ch: applied.append("experience_sizing")

    old="""    candidates=_candidate_book_signal_first(summary)
    impulse_candidates=_best_impulse_by_asset(summary)"""
    new="""    candidates=_candidate_book_v84(summary)
    impulse_candidates=_best_impulse_by_asset(summary)"""
    dst,ch=_replace_once(dst,old,new,"v84 portfolio routing")
    if ch: applied.append("portfolio_routing")

    old="""    if z and z['direction']!=direction:
        _close_or_reduce(c,p,name,z,price,0.0,nav,ts,'DIRECTION_FLIP')
        z=None"""
    new="""    if z and z['direction']!=direction:
        if not bool(row.get('_flip_confirmed',False)):
            return
        _close_or_reduce(c,p,name,z,price,0.0,nav,ts,'V84_CONFIRMED_DIRECTION_FLIP')
        z=None"""
    dst,ch=_replace_once(dst,old,new,"v84 portfolio flip guard")
    if ch: applied.append("flip_guard")

    old='def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate):'
    new='def _step_one(c,name,policy,candidates,prices,ruonia,usdrub,ts,commission_rate,summary=None):'
    dst,ch=_replace_once(dst,old,new,"v84.2 management summary")
    if ch: applied.append("management_summary")

    old="""    # Missing candidate is NOT an immediate exit anymore.\n    # Keep the current fraction for the first soft-deterioration cycle; the\n    # close/reduce loop below decides whether invalidation is hard or persistent.\n    for z in pos:\n        if z['asset'] not in targets:\n            px=float(prices.get(z['asset'],z['last_price']))\n            targets[z['asset']]=abs(float(z['units'])*px)/max(nav,1.0)"""
    new="""    # Missing a fresh candidate is soft deterioration, not an exit.\n    # Hold current exposure unless the execution-horizon row has a hard thesis invalidation.\n    for z in pos:\n        if z['asset'] not in targets:\n            px=float(prices.get(z['asset'],z['last_price']))\n            cur=abs(float(z['units'])*px)/max(nav,1.0)\n            mgmt=_v842_management_row(summary,z)\n            targets[z['asset']]=0.0 if _v842_hard_thesis_exit(mgmt) else cur"""
    dst,ch=_replace_once(dst,old,new,"v84.2 hold without fresh signal")
    if ch: applied.append("hold_without_signal")

    old="""        row=candidates.get(z['asset']); target=float(targets.get(z['asset'],0.0)); px=float(prices.get(z['asset'],z['last_price']))\n        wrong_dir=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])\n        # Structural stop remains an immediate hard invalidation.\n        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))\n        current_frac=abs(float(z['units'])*px)/max(nav,1.0)\n        raw_target=float(_desired_fraction(row,policy,dd)) if row else 0.0\n        fs=_soft_failure_state(c,name,z,row,raw_target,current_frac)\n        if stop_hit:\n            _close_or_reduce(c,p,name,z,px,0.0,nav,ts,'STRUCTURAL_STOP')\n        elif fs['hard']:\n            _close_or_reduce(c,p,name,z,px,0.0,nav,ts,fs['reason'])\n        elif fs['confirmed_soft']:\n            # Soft deterioration has persisted across two portfolio cycles.\n            _close_or_reduce(c,p,name,z,px,raw_target,nav,ts,'SOFT_INVALIDATION_CONFIRMED')\n        else:\n            # First soft failure: preserve the position. Do not churn.\n            targets[z['asset']]=current_frac"""
    new="""        row=candidates.get(z['asset']); target=float(targets.get(z['asset'],0.0)); px=float(prices.get(z['asset'],z['last_price']))\n        original_target=target\n        current_frac=abs(float(z['units'])*px)/max(nav,1.0)\n        mgmt=_v842_management_row(summary,z)\n        hard_exit=_v842_hard_thesis_exit(mgmt)\n        opposite=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])\n        confirmed_flip=bool(opposite and row.get('_flip_confirmed',False))\n        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))\n        if opposite and not confirmed_flip and not hard_exit and not stop_hit:\n            target=current_frac; targets[z['asset']]=current_frac\n        if row and not opposite and target<=0 and not hard_exit and rg.get('new_risk',True):\n            target=current_frac; targets[z['asset']]=current_frac\n        if confirmed_flip or hard_exit or stop_hit or rg.get('new_risk') is False:\n            target=0.0\n            if rg.get('new_risk') is False:\n                targets[z['asset']]=0.0\n            elif row and opposite:\n                # Close old thesis, then preserve the validated opposite target so signal-first can open the new direction in the same cycle.\n                targets[z['asset']]=original_target\n            else:\n                targets[z['asset']]=0.0\n        if target<current_frac-0.025:\n            reason='STOP' if stop_hit else 'V842_CONFIRMED_DIRECTION_FLIP' if confirmed_flip else 'HARD_THESIS_INVALIDATION' if hard_exit else 'RISK_HARD_STOP' if rg.get('new_risk') is False else 'SOFT_SIZE_REDUCTION'\n            _close_or_reduce(c,p,name,z,px,target,nav,ts,reason)"""
    dst,ch=_replace_once(dst,old,new,"v84.2 active position exit semantics")
    if ch: applied.append("active_exit_semantics")

    old="""'canonical_setup_id':canonical_setup_id,"""
    new="""'canonical_setup_id':canonical_setup_id,
                 'experience_decision':plan.get('experience_decision'),
                 'setup_memory':plan.get('setup_memory'),
                 'adaptive_regime_policy':plan.get('adaptive_regime_policy'),
                 'execution_policy':plan.get('execution_policy'),"""
    dst,ch=_replace_once(dst,old,new,"persist v84 paper trade policy")
    if ch: applied.append("persist_policy")

    old='results.append(_step_one(c,name,pol,book,prices,ruonia,usdrub,ts,commission_rate))'
    new='results.append(_step_one(c,name,pol,book,prices,ruonia,usdrub,ts,commission_rate,summary))'
    dst,ch=_replace_once(dst,old,new,"v84.2 pass summary to management")
    if ch: applied.append("management_summary_call")

    old="""'target_fraction':sf.get('fraction'),'reason':sf.get('reason')})"""
    new="""'target_fraction':sf.get('fraction'),'reason':sf.get('reason'),
                    'supporting_horizons':row.get('_supporting_horizons'),
                    'direction_support':row.get('_direction_support'),
                    'flip_confirmed':row.get('_flip_confirmed'),
                    'experience_decision':sf.get('experience_decision') or plan.get('execution_policy')})"""
    dst,ch=_replace_once(dst,old,new,"v84 admission trace")
    if ch: applied.append("admission_trace")

    old="""                           unified_execution=True)"""
    new="""                           unified_execution=True,experience_weighted=True,adaptive_regime=True,v84_execution=True,v842_audited=True)"""
    dst,ch=_replace_once(dst,old,new,"v84 portfolio telemetry")
    if ch: applied.append("telemetry")

    if dst!=src:
        _write(PORTFOLIO_TARGET,dst)
    return applied


def _sync_v70():
    if not V70_TARGET.exists():
        return False
    src=_read(V70_TARGET)
    dst=src.replace(V80_INTEL,V84_INTEL)
    if dst!=src:
        _write(V70_TARGET,dst)
        return True
    return False


def _verify():
    intel=_read(TARGET); port=_read(PORTFOLIO_TARGET)
    checks={
        'intel_version':V84_INTEL in intel,
        'portfolio_version':V84_PORT in port,
        'outcome_learning':'def refresh_experience_lessons(' in intel,
        'setup_memory':'def setup_memory_profile(' in intel,
        'regime_policy':'def adaptive_regime_policy(' in intel,
        'execution_policy':'def execution_policy_v84(' in intel,
        'rejected_signal':"'rejected_signal_lesson'" in intel,
        'abstention_learning':"'abstention_lesson'" in intel,
        'counterfactual':"'counterfactual_horizon_utility'" in intel,
        'flip_guard':'v84_direction_flip_confirmed(x,current_direction)' in intel,
        'background_learning':'xp=refresh_experience_lessons()' in intel,
        'bounded_learning_sql':'def _bounded_completed_episode_rows(' in intel and 'bounded_recent_episode_window' in intel,
        'learning_index':'def learning_index_v2()' in intel,
        'portfolio_candidate_book':'def _candidate_book_v84(summary):' in port,
        'portfolio_v84_routing':'candidates=_candidate_book_v84(summary)' in port,
        'portfolio_flip_guard':"V84_CONFIRMED_DIRECTION_FLIP" in port,
        'fast_memory_no_db':"fast path reads only a precomputed background experience snapshot" in intel,
        'single_sizing_authority':"DECISION_LAYER_TARGET_THEN_PORTFOLIO_RISK" in intel and "SIGNAL_FIRST_V842" in port,
        'hold_without_signal':"Missing a fresh candidate is soft deterioration, not an exit." in port,
        'active_exit_semantics':"V842_CONFIRMED_DIRECTION_FLIP" in port and "HARD_THESIS_INVALIDATION" in port and "preserve the validated opposite target" in port,
        'experience_direction_prior':"experience_prior=experience_direction_prior(asset,horizon,f)" in intel,
        'ndx_fail_closed':"ndx_verification_fail_closed" in intel,
        'pg_event_reuse':"_v842_pg_event_conn" in intel,
        'analog_horizon_index':"_v842_vectors_by_horizon" in intel,
        'strict_direction_sample':"prior_min_n=max(40" in intel and "prior_min_eff=max(20.0" in intel,
        'exact_execution_memory':"mem_status in ('EXECUTION_READY','WEIGHT_READY')" in intel,
        'premature_exit_attribution':"RIGHT_DIRECTION_PREMATURE_EXIT" in intel,
        'learning_index_expectancy_guard':"profit_factor" in intel and "average P&L and profit factor may not deteriorate" in intel,
        'signal_first_floor':'f=max(0.05,f)' in port,
        'unified_execution':'unified_execution=True' in port,
        'v84_telemetry':'v84_execution=True' in port,
        'hashlib':("hashlib.sha256" not in port or "import hashlib" in "\n".join(port.splitlines()[:30])),
    }
    failed=[k for k,v in checks.items() if not v]
    if failed:
        raise RuntimeError("v84 verification failed: "+", ".join(failed))
    compile(intel,str(TARGET),'exec')
    compile(port,str(PORTFOLIO_TARGET),'exec')
    return checks


def _run():
    try:
        if _already_v84():
            _ensure_hashlib()
            _verify()
            print("[VERITAS BOOTSTRAP] v84.2 READY: idempotent=true; adaptive_experience_execution=true",flush=True)
            return

        if not _already_v80():
            _execute_v80()
        _ensure_hashlib()
        if not _already_v80():
            raise RuntimeError("v80 foundation not established")

        ia=_patch_intelligence()
        pa=_patch_portfolio()
        _ensure_hashlib()
        v70=_sync_v70()
        _verify()

        print(
            "[VERITAS BOOTSTRAP] v84.2 VERIFIED: "
            f"intelligence={','.join(ia)}; portfolio={','.join(pa)}; v70_sync={str(v70).lower()}",
            flush=True
        )
    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v84.2 FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,flush=True
        )


_run()
