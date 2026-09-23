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
V84_INTEL="veritas-max-product-v84.0-adaptive-experience-execution"
V84_PORT="veritas-portfolio-v8-v84-adaptive-execution"

START_MARKER="    # ----- v78.1 Rule & Experience Arbitration -----"
END_MARKER="    # ----- v79.0 Portfolio Trade Integrity -----"
INSERT_MARKER="    # ----- v79.0 Trade Integrity / Win-Rate Layer -----"

V84_HELPER_BLOCK='\n# =========================\n# VERITAS v81-v84 ADAPTIVE EXPERIENCE EXECUTION\n# v81: outcome learning + error attribution + rejected signals\n# v82: hierarchical setup memory\n# v83: regime-conditioned adaptive policy\n# v84: entry / stop / exit optimization\n# =========================\n\nEXPERIENCE_MIN_SIZE_N = max(6, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_SIZE_N\',\'8\')))\nEXPERIENCE_MIN_EXEC_N = max(12, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_EXEC_N\',\'20\')))\nEXPERIENCE_MIN_WEIGHT_N = max(20, int(os.getenv(\'VERITAS_EXPERIENCE_MIN_WEIGHT_N\',\'40\')))\nEXPERIENCE_DECAY_HALF_LIFE_DAYS = max(30.0, float(os.getenv(\'VERITAS_EXPERIENCE_DECAY_HALF_LIFE_DAYS\',\'90\')))\nEXPERIENCE_MAX_EVENTS = max(500, min(20000, int(os.getenv(\'VERITAS_EXPERIENCE_MAX_EVENTS\',\'6000\'))))\nSETUP_MEMORY_CACHE_SECONDS = max(30, int(os.getenv(\'VERITAS_SETUP_MEMORY_CACHE_SECONDS\',\'60\')))\nV84_DIRECTION_PRIOR_CAP = min(0.05, max(0.01, float(os.getenv(\'VERITAS_V84_DIRECTION_PRIOR_CAP\',\'0.04\'))))\nV84_STOP_WIDEN_CAP = min(1.40, max(1.0, float(os.getenv(\'VERITAS_V84_STOP_WIDEN_CAP\',\'1.30\'))))\n\n\ndef _v84_json(x):\n    if isinstance(x,dict): return x\n    if not x: return {}\n    try: return json.loads(x)\n    except Exception: return {}\n\n\ndef _v84_setup_family(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    piv=row.get(\'impulse_pivot_break\') or {}\n    rev=row.get(\'tactical_reversal\') or {}\n    rng=row.get(\'range_retest_breakout\') or {}\n    bq=(row.get(\'institutional_signal\') or {}).get(\'breakout_quality\') or {}\n    if piv.get(\'active\'): return \'IMPULSE_PIVOT_BREAK\'\n    if rev.get(\'active\'): return \'TACTICAL_REVERSAL\'\n    if rng.get(\'active\'): return \'RANGE_RETEST_BREAKOUT\'\n    if str(bq.get(\'state\') or \'\') in (\'EARLY_BREAKOUT\',\'CONFIRMED_BREAKOUT\'): return \'BREAKOUT\'\n    if str(plan.get(\'regime_shift_state\') or \'\') in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\'): return \'REGIME_SHIFT\'\n    return \'TREND\'\n\n\ndef _v84_entry_state(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    eq=str(plan.get(\'entry_quality\') or row.get(\'entry_quality\') or \'\')\n    life=str(plan.get(\'structure_lifecycle\') or \'\')\n    rng=row.get(\'range_retest_breakout\') or {}\n    if rng.get(\'active\') and str(rng.get(\'state\') or \'\')==\'RETEST_ENTRY\': return \'RETEST\'\n    if \'LATE\' in eq or plan.get(\'late_entry\'): return \'LATE\'\n    if \'BREAKOUT\' in eq or plan.get(\'fresh_breakout\'): return \'BREAKOUT\'\n    if life in (\'CONFIRMATION\',\'EXTENSION\') or \'CONFIRMED\' in eq: return \'CONFIRMED\'\n    if life in (\'EARLY\',\'APPROACH\') or \'EARLY\' in eq: return \'EARLY\'\n    return \'NORMAL\'\n\n\ndef _v84_regime_bucket(row=None, plan=None):\n    row=row or {}\n    plan=plan or row.get(\'trade_plan\') or {}\n    rg=str(row.get(\'regime\') or plan.get(\'regime\') or \'UNKNOWN\').upper()\n    shift=str(plan.get(\'regime_shift_state\') or \'\').upper()\n    if shift in (\'NEW_REGIME_PROVISIONAL\',\'TRANSITION\',\'OLD_REGIME_WEAKENING\') or \'TRANSITION\' in rg:\n        return \'TRANSITION\'\n    if \'PANIC\' in rg or \'STRESS\' in rg or \'HIGH_VOL\' in rg:\n        if \'RANGE\' in rg: return \'HIGH_VOL_RANGE\'\n        return \'HIGH_VOL_TREND\'\n    if \'RANGE\' in rg or \'MEAN_REVERT\' in rg: return \'RANGE\'\n    if \'UPTREND\' in rg or \'DOWNTREND\' in rg or \'TREND\' in rg: return \'TREND\'\n    return \'ADAPTIVE\'\n\n\ndef _v84_horizon_state(row=None):\n    row=row or {}\n    hs=row.get(\'horizon_structure\') or {}\n    return str(hs.get(\'state\') or \'UNKNOWN\')\n\n\ndef _v84_event_weight(ts, base=1.0):\n    now_dt=datetime.now(timezone.utc)\n    if isinstance(ts,str):\n        try: ts=datetime.fromisoformat(ts.replace(\'Z\',\'+00:00\'))\n        except Exception: ts=None\n    if ts is not None and getattr(ts,\'tzinfo\',None) is None:\n        ts=ts.replace(tzinfo=timezone.utc)\n    age=max(0.0,(now_dt-ts).total_seconds()/86400.0) if ts is not None else 0.0\n    return float(base)*math.exp(-math.log(2.0)*age/EXPERIENCE_DECAY_HALF_LIFE_DAYS)\n\n\ndef experience_direction_prior(asset,horizon,f):\n    # Bounded analog prior. It refines marginal scores but never bypasses hard gates.\n    try:\n        row=tradeability_analog_stats(asset,horizon,f,\'LONG\')\n        n=int(row.get(\'raw_n\') or 0); en=float(row.get(\'effective_n\') or 0.0)\n        p=row.get(\'positive_trade_probability\')\n        if p is None or n<TRADEABILITY_MIN_RAW_N or en<TRADEABILITY_MIN_EFFECTIVE_N:\n            return {\'status\':\'BUILDING\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                    \'raw_n\':n,\'effective_n\':en,\'p_long\':p}\n        p=float(p)\n        adj=clip((p-0.50)*0.16,-V84_DIRECTION_PRIOR_CAP,V84_DIRECTION_PRIOR_CAP)\n        return {\'status\':\'ACTIVE\',\'decision_influence\':True,\'score_adjustment\':round(adj,5),\n                \'raw_n\':n,\'effective_n\':round(en,2),\'p_long\':round(p,4),\n                \'principle\':\'bounded analog prior; hard gates remain absolute\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'decision_influence\':False,\'score_adjustment\':0.0,\n                \'error\':f\'{type(ex).__name__}: {ex}\'}\n\n\ndef refresh_experience_lessons(limit=400):\n    # Persist canonical trade lessons, rejected directional signals and abstentions.\n    if not pg_enabled():\n        return {\'status\':\'POSTGRES_REQUIRED\',\'trade_lessons\':0,\'rejected_lessons\':0,\'abstention_lessons\':0}\n    limit=max(50,min(1500,int(limit)))\n    trade_lessons=rejected_lessons=abstention_lessons=0\n    errors=[]\n\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT t.trade_id,t.setup_id,t.asset,t.horizon,t.direction,t.status,\n                                     t.created_at,t.closed_at,t.total_pnl_fraction,t.high_price,t.low_price,\n                                     t.avg_entry_price,t.stop_price,t.add_count,t.reduce_count,\n                                     s.payload setup_payload,\n                                     d.entity_key,d.payload decision_payload,o.payload outcome_payload\n                              FROM shadow_trades t\n                              JOIN trade_setups s ON s.setup_id=t.setup_id\n                              LEFT JOIN LATERAL (\n                                SELECT entity_key,payload FROM ledger_events\n                                WHERE event_type=\'decision\' AND asset=t.asset AND horizon=t.horizon\n                                  AND event_ts<=t.created_at\n                                ORDER BY event_ts DESC LIMIT 1\n                              ) d ON TRUE\n                              LEFT JOIN ledger_events o\n                                ON o.entity_key=d.entity_key AND o.event_type=\'outcome\'\n                              WHERE t.status<>\'ACTIVE\' AND t.total_pnl_fraction IS NOT NULL\n                              ORDER BY t.closed_at DESC NULLS LAST LIMIT %s""",(limit,)).fetchall()\n        for rr in rows:\n            x=dict(rr)\n            sp=_v84_json(x.get(\'setup_payload\')); dp=_v84_json(x.get(\'decision_payload\')); op=_v84_json(x.get(\'outcome_payload\'))\n            pnl=float(x.get(\'total_pnl_fraction\') or 0.0)\n            fr=op.get(\'forward_return\')\n            sr=None if fr is None else (float(fr) if x[\'direction\']==\'LONG\' else -float(fr))\n            rowctx={**(dp.get(\'features\') or {}),**dp}\n            plan=dp.get(\'trade_plan\') or sp\n            family=str(sp.get(\'setup_family\') or _v84_setup_family(rowctx,plan))\n            regime=_v84_regime_bucket(rowctx,plan)\n            entry_state=_v84_entry_state(rowctx,plan)\n            hstate=_v84_horizon_state(rowctx)\n\n            if pnl>0:\n                label=\'GOOD_EXECUTION\' if sr is None or sr>=0 else \'TACTICAL_WIN_AGAINST_HORIZON\'\n            elif sr is not None and sr<0:\n                label=\'DIRECTION_ERROR\'\n            elif sr is not None and sr>0 and str(x.get(\'status\'))==\'STOP\':\n                label=\'RIGHT_DIRECTION_STOP_ERROR\'\n            elif sr is not None and sr>0 and entry_state==\'LATE\':\n                label=\'RIGHT_DIRECTION_LATE_ENTRY\'\n            elif sr is not None and sr>0:\n                label=\'RIGHT_DIRECTION_EXECUTION_ERROR\'\n            else:\n                label=\'NEGATIVE_EXECUTION\'\n\n            entry=float(x.get(\'avg_entry_price\') or 0.0)\n            high=x.get(\'high_price\'); low=x.get(\'low_price\')\n            mfe_trade=mae_trade=None\n            if entry>0 and high is not None and low is not None:\n                if x[\'direction\']==\'LONG\':\n                    mfe_trade=float(high)/entry-1; mae_trade=float(low)/entry-1\n                else:\n                    mfe_trade=entry/float(low)-1 if float(low)>0 else None\n                    mae_trade=entry/float(high)-1 if float(high)>0 else None\n\n            payload={\n                \'trade_id\':x[\'trade_id\'],\'setup_id\':x.get(\'setup_id\'),\n                \'setup_family\':family,\'regime_bucket\':regime,\'entry_state\':entry_state,\n                \'horizon_state\':hstate,\'direction\':x[\'direction\'],\'label\':label,\n                \'profitable\':bool(pnl>0),\'actual_pnl_fraction\':pnl,\n                \'horizon_signed_return\':sr,\'outcome_mfe\':op.get(\'mfe\'),\'outcome_mae\':op.get(\'mae\'),\n                \'trade_mfe\':mfe_trade,\'trade_mae\':mae_trade,\n                \'add_count\':int(x.get(\'add_count\') or 0),\'reduce_count\':int(x.get(\'reduce_count\') or 0),\n                \'counterfactual_no_trade_utility\':0.0,\n                \'counterfactual_horizon_utility\':sr,\n                \'counterfactual_supported\':[\'NO_TRADE\',\'HOLD_TO_HORIZON\'] if sr is not None else [\'NO_TRADE\'],\n                \'counterfactual_not_identifiable\':[\'DELAYED_ENTRY_WITH_PATH_ORDER\',\'ALTERNATE_STOP_WITH_PATH_ORDER\'],\n                \'source\':\'CANONICAL_SHADOW_TRADE\',\'learning_weight\':1.0,\n            }\n            pg_event(\'experience_lesson\',x[\'trade_id\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'closed_at\') or now())\n            trade_lessons+=1\n    except Exception as ex:\n        errors.append(f\'trades:{type(ex).__name__}:{ex}\')\n\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT d.entity_key,d.event_ts,d.asset,d.horizon,d.payload dp,o.payload op\n                              FROM ledger_events d\n                              JOIN ledger_events o ON o.entity_key=d.entity_key AND o.event_type=\'outcome\'\n                              WHERE d.event_type=\'decision\'\n                              ORDER BY d.event_ts DESC LIMIT %s""",(limit*5,)).fetchall()\n        for rr in rows:\n            x=dict(rr); dp=_v84_json(x.get(\'dp\')); op=_v84_json(x.get(\'op\'))\n            research=str(dp.get(\'research_decision\') or dp.get(\'decision\') or \'NO_TRADE\')\n            executed=str(dp.get(\'decision\') or \'NO_TRADE\')\n            fr=op.get(\'forward_return\')\n            if fr is None: continue\n            fr=float(fr)\n\n            if research in (\'LONG\',\'SHORT\') and executed not in (\'LONG\',\'SHORT\'):\n                elig=dp.get(\'execution_eligibility\') or {}; plan=dp.get(\'trade_plan\') or {}\n                hard_veto=bool(\n                    (elig and not bool(elig.get(\'eligible\',True))) or\n                    ((plan.get(\'trade_integrity\') or {}).get(\'hard_invalidation\')) or\n                    (((plan.get(\'rule_arbitration\') or {}).get(\'hard_veto\') or {}).get(\'decision\')==\'VETO\')\n                )\n                sr=fr if research==\'LONG\' else -fr\n                significant=abs(fr)>=_no_trade_miss_threshold(x.get(\'horizon\'))\n                label=\'REJECTED_WINNER\' if sr>0 and significant else (\n                      \'REJECTED_LOSER\' if sr<0 and significant else \'REJECTED_NEUTRAL\')\n                rowctx={**(dp.get(\'features\') or {}),**dp}\n                payload={\n                    \'direction\':research,\'label\':label,\'signed_return\':sr,\n                    \'setup_family\':_v84_setup_family(rowctx,plan),\n                    \'regime_bucket\':_v84_regime_bucket(rowctx,plan),\n                    \'entry_state\':_v84_entry_state(rowctx,plan),\n                    \'horizon_state\':_v84_horizon_state(rowctx),\n                    \'hard_gate\':hard_veto,\n                    \'learning_weight\':0.0 if hard_veto else 1.0,\n                    \'execution_eligibility\':elig,\'reason\':plan.get(\'reason\'),\n                    \'source\':\'REJECTED_DIRECTIONAL_SIGNAL\'\n                }\n                pg_event(\'rejected_signal_lesson\',x[\'entity_key\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'event_ts\') or now())\n                rejected_lessons+=1\n\n            if research==\'NO_TRADE\':\n                th=_no_trade_miss_threshold(x.get(\'horizon\'))\n                label=\'GOOD_ABSTENTION\' if abs(fr)<th else \'MISSED_LARGE_MOVE\'\n                payload={\'label\':label,\'forward_return\':fr,\'threshold\':th,\n                         \'learning_weight\':0.5,\'source\':\'ABSTENTION_OUTCOME\'}\n                pg_event(\'abstention_lesson\',x[\'entity_key\'],payload,x[\'asset\'],x[\'horizon\'],x.get(\'event_ts\') or now())\n                abstention_lessons+=1\n    except Exception as ex:\n        errors.append(f\'decisions:{type(ex).__name__}:{ex}\')\n\n    try:\n        setup_memory_board._cache=None\n    except Exception:\n        pass\n    return {\'status\':\'OK\' if not errors else \'DEGRADED\',\n            \'trade_lessons\':trade_lessons,\'rejected_lessons\':rejected_lessons,\n            \'abstention_lessons\':abstention_lessons,\'errors\':errors}\n\n\ndef setup_memory_board(force=False):\n    # Hierarchical setup memory. Exact states get most weight, broad families provide shrinkage.\n    cache=getattr(setup_memory_board,\'_cache\',None)\n    if cache and not force and time.time()-cache[0] < SETUP_MEMORY_CACHE_SECONDS:\n        return cache[1]\n    if not pg_enabled():\n        return {\'status\':\'POSTGRES_REQUIRED\',\'items\':[]}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT event_type,event_ts,asset,horizon,payload\n                              FROM ledger_events\n                              WHERE event_type IN (\'experience_lesson\',\'rejected_signal_lesson\')\n                              ORDER BY event_ts DESC LIMIT %s""",(EXPERIENCE_MAX_EVENTS,)).fetchall()\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'items\':[],\'error\':f\'{type(ex).__name__}: {ex}\'}\n\n    buckets={}\n    def touch(key):\n        return buckets.setdefault(key,{\'n\':0,\'w\':0.0,\'wins\':0.0,\'pnl\':0.0,\'stop_err\':0.0,\n                                       \'dir_err\':0.0,\'late_err\':0.0,\'good\':0.0,\n                                       \'rej_win\':0.0,\'rej_loss\':0.0,\'rej_n\':0})\n\n    for rr in rows:\n        x=dict(rr); p=_v84_json(x.get(\'payload\'))\n        d=str(p.get(\'direction\') or \'NO_TRADE\')\n        if d not in (\'LONG\',\'SHORT\'): continue\n        fam=str(p.get(\'setup_family\') or \'UNKNOWN\')\n        reg=str(p.get(\'regime_bucket\') or \'ADAPTIVE\')\n        ent=str(p.get(\'entry_state\') or \'NORMAL\')\n        asset=str(x.get(\'asset\')); h=str(x.get(\'horizon\'))\n        base=float(p.get(\'learning_weight\',1.0) or 0.0)\n        w=_v84_event_weight(x.get(\'event_ts\'),base)\n        if w<=0: continue\n\n        keys=[\n            (\'EXACT\',asset,h,fam,d,reg,ent),\n            (\'ASSET_FAMILY\',asset,h,fam,d,\'*\',\'*\'),\n            (\'REGIME_FAMILY\',\'*\',h,fam,d,reg,\'*\'),\n            (\'FAMILY\',\'*\',h,fam,d,\'*\',\'*\'),\n        ]\n        for key in keys:\n            z=touch(key)\n            if x.get(\'event_type\')==\'experience_lesson\':\n                z[\'n\']+=1; z[\'w\']+=w\n                if p.get(\'profitable\'): z[\'wins\']+=w\n                z[\'pnl\']+=w*float(p.get(\'actual_pnl_fraction\') or 0.0)\n                label=str(p.get(\'label\') or \'\')\n                if label==\'RIGHT_DIRECTION_STOP_ERROR\': z[\'stop_err\']+=w\n                if label==\'DIRECTION_ERROR\': z[\'dir_err\']+=w\n                if label==\'RIGHT_DIRECTION_LATE_ENTRY\': z[\'late_err\']+=w\n                if label.startswith(\'GOOD_\') or label==\'TACTICAL_WIN_AGAINST_HORIZON\': z[\'good\']+=w\n            else:\n                z[\'rej_n\']+=1\n                if p.get(\'label\')==\'REJECTED_WINNER\': z[\'rej_win\']+=w\n                elif p.get(\'label\')==\'REJECTED_LOSER\': z[\'rej_loss\']+=w\n\n    items=[]\n    for key,z in buckets.items():\n        level,asset,h,fam,d,reg,ent=key\n        en=float(z[\'w\']); n=int(z[\'n\'])\n        pwin=(z[\'wins\']+5.0)/(en+10.0) if en>0 else None\n        rden=z[\'rej_win\']+z[\'rej_loss\']\n        items.append({\n            \'level\':level,\'asset\':asset,\'horizon\':h,\'setup_family\':fam,\'direction\':d,\n            \'regime_bucket\':reg,\'entry_state\':ent,\'n\':n,\'effective_n\':round(en,2),\n            \'posterior_win_rate\':None if pwin is None else round(pwin,4),\n            \'weighted_avg_pnl\':round(z[\'pnl\']/en,6) if en else None,\n            \'stop_error_rate\':round(z[\'stop_err\']/en,4) if en else None,\n            \'direction_error_rate\':round(z[\'dir_err\']/en,4) if en else None,\n            \'late_entry_error_rate\':round(z[\'late_err\']/en,4) if en else None,\n            \'good_execution_rate\':round(z[\'good\']/en,4) if en else None,\n            \'rejected_n\':z[\'rej_n\'],\n            \'rejected_winner_rate\':round(z[\'rej_win\']/rden,4) if rden else None,\n            \'status\':\'WEIGHT_READY\' if en>=EXPERIENCE_MIN_WEIGHT_N else (\n                     \'EXECUTION_READY\' if en>=EXPERIENCE_MIN_EXEC_N else (\n                     \'SIZE_READY\' if en>=EXPERIENCE_MIN_SIZE_N else \'BUILDING\'))\n        })\n    items.sort(key=lambda x:(x[\'level\']!=\'EXACT\',x[\'status\']==\'BUILDING\',-float(x.get(\'effective_n\') or 0)))\n    out={\'status\':\'OK\',\'items\':items,\n         \'thresholds\':{\'size\':EXPERIENCE_MIN_SIZE_N,\'execution\':EXPERIENCE_MIN_EXEC_N,\'weight\':EXPERIENCE_MIN_WEIGHT_N},\n         \'hierarchy\':[\'EXACT\',\'ASSET_FAMILY\',\'REGIME_FAMILY\',\'FAMILY\'],\n         \'principle\':\'exact setup/regime experience dominates; broader pools only shrink small samples\'}\n    setup_memory_board._cache=(time.time(),out)\n    return out\n\n\ndef setup_memory_profile(asset,horizon,row,direction,plan):\n    board=setup_memory_board()\n    fam=_v84_setup_family(row,plan); reg=_v84_regime_bucket(row,plan); ent=_v84_entry_state(row,plan)\n    wanted=[\n        (\'EXACT\',asset,horizon,fam,direction,reg,ent,1.00),\n        (\'ASSET_FAMILY\',asset,horizon,fam,direction,\'*\',\'*\',0.70),\n        (\'REGIME_FAMILY\',\'*\',horizon,fam,direction,reg,\'*\',0.55),\n        (\'FAMILY\',\'*\',horizon,fam,direction,\'*\',\'*\',0.35),\n    ]\n    selected=[]\n    for lvl,a,h,f,d,r,e,shrink in wanted:\n        x=next((q for q in board.get(\'items\',[]) if q.get(\'level\')==lvl and q.get(\'asset\')==a\n                and q.get(\'horizon\')==h and q.get(\'setup_family\')==f and q.get(\'direction\')==d\n                and q.get(\'regime_bucket\')==r and q.get(\'entry_state\')==e),None)\n        if x and float(x.get(\'effective_n\') or 0)>0:\n            selected.append((x,shrink))\n    if not selected:\n        return {\'status\':\'BUILDING\',\'decision_influence\':False,\'setup_family\':fam,\n                \'regime_bucket\':reg,\'entry_state\':ent,\'effective_n\':0.0}\n\n    num=den=0.0; pnl_num=0.0; stop_num=late_num=dir_num=rej_num=rej_den=0.0; eff=0.0\n    evidence=[]\n    for x,shrink in selected:\n        en=float(x.get(\'effective_n\') or 0.0)\n        w=min(40.0,en)*shrink\n        p=x.get(\'posterior_win_rate\')\n        if p is not None: num+=float(p)*w; den+=w\n        if x.get(\'weighted_avg_pnl\') is not None: pnl_num+=float(x[\'weighted_avg_pnl\'])*w\n        stop_num+=float(x.get(\'stop_error_rate\') or 0.0)*w\n        late_num+=float(x.get(\'late_entry_error_rate\') or 0.0)*w\n        dir_num+=float(x.get(\'direction_error_rate\') or 0.0)*w\n        rw=x.get(\'rejected_winner_rate\')\n        if rw is not None: rej_num+=float(rw)*w; rej_den+=w\n        eff=max(eff,en if x.get(\'level\')==\'EXACT\' else en*shrink)\n        evidence.append({\'level\':x.get(\'level\'),\'effective_n\':en,\'pwin\':p})\n    pwin=num/den if den else None\n    level=\'OBSERVE\'\n    if eff>=EXPERIENCE_MIN_WEIGHT_N: level=\'WEIGHT_READY\'\n    elif eff>=EXPERIENCE_MIN_EXEC_N: level=\'EXECUTION_READY\'\n    elif eff>=EXPERIENCE_MIN_SIZE_N: level=\'SIZE_READY\'\n    return {\'status\':level,\'decision_influence\':level!=\'OBSERVE\',\n            \'setup_family\':fam,\'regime_bucket\':reg,\'entry_state\':ent,\n            \'effective_n\':round(eff,2),\'posterior_win_rate\':None if pwin is None else round(pwin,4),\n            \'weighted_avg_pnl\':round(pnl_num/den,6) if den else None,\n            \'stop_error_rate\':round(stop_num/den,4) if den else None,\n            \'late_entry_error_rate\':round(late_num/den,4) if den else None,\n            \'direction_error_rate\':round(dir_num/den,4) if den else None,\n            \'rejected_winner_rate\':round(rej_num/rej_den,4) if rej_den else None,\n            \'evidence\':evidence}\n\n\ndef adaptive_regime_policy(asset,horizon,row,direction,plan,memory):\n    reg=_v84_regime_bucket(row,plan)\n    route=regime_route_for(asset,horizon,row.get(\'regime\'),direction)\n    size=1.0; stop=1.0; confirmation=\'NORMAL\'; add_allowed=True; exit_guard=\'NORMAL\'\n\n    if reg==\'TREND\':\n        size=1.05; stop=1.05; confirmation=\'TREND_CONTINUATION\'; exit_guard=\'STRONG\'\n    elif reg==\'RANGE\':\n        size=0.80; stop=0.95; confirmation=\'RETEST_OR_EDGE\'; add_allowed=False; exit_guard=\'NORMAL\'\n    elif reg==\'HIGH_VOL_TREND\':\n        size=0.75; stop=1.18; confirmation=\'STAGED\'; exit_guard=\'STRONG\'\n    elif reg==\'HIGH_VOL_RANGE\':\n        size=0.65; stop=1.12; confirmation=\'RETEST_ONLY\'; add_allowed=False; exit_guard=\'NORMAL\'\n    elif reg==\'TRANSITION\':\n        size=0.65; stop=1.10; confirmation=\'PROBE_THEN_CONFIRM\'; add_allowed=False; exit_guard=\'STRONG\'\n\n    if route.get(\'decision_influence\'):\n        size*=clip(float(route.get(\'position_multiplier\') or 1.0),0.70,1.20)\n\n    p=(memory or {}).get(\'posterior_win_rate\')\n    if (memory or {}).get(\'decision_influence\') and p is not None:\n        p=float(p)\n        if p>=0.68: size*=1.10\n        elif p<=0.44: size*=0.75\n\n    size=clip(size,0.50,1.25)\n    stop=clip(stop,0.90,V84_STOP_WIDEN_CAP)\n    return {\'regime_bucket\':reg,\'route\':route.get(\'route\'),\'route_status\':route.get(\'status\'),\n            \'decision_influence\':True,\'size_multiplier\':round(size,4),\n            \'stop_multiplier\':round(stop,4),\'confirmation_mode\':confirmation,\n            \'add_allowed\':add_allowed,\'exit_guard\':exit_guard,\n            \'direction_override\':False}\n\n\ndef execution_policy_v84(asset,horizon,row,direction,plan,memory,regime_policy):\n    entry_state=_v84_entry_state(row,plan)\n    p=(memory or {}).get(\'posterior_win_rate\')\n    stop_err=float((memory or {}).get(\'stop_error_rate\') or 0.0)\n    late_err=float((memory or {}).get(\'late_entry_error_rate\') or 0.0)\n    rej=float((memory or {}).get(\'rejected_winner_rate\') or 0.0)\n    eff=float((memory or {}).get(\'effective_n\') or 0.0)\n\n    entry_mode=\'IMMEDIATE_PROBE\'\n    if entry_state==\'LATE\':\n        entry_mode=\'PULLBACK_OR_MIN_PROBE\'\n    elif entry_state==\'RETEST\':\n        entry_mode=\'RETEST_STAGED\'\n    elif entry_state==\'BREAKOUT\':\n        entry_mode=\'BREAKOUT_PROBE_THEN_CONFIRM\'\n    elif entry_state==\'CONFIRMED\':\n        entry_mode=\'CONFIRMED_SCALE\'\n    elif (regime_policy or {}).get(\'confirmation_mode\') in (\'STAGED\',\'PROBE_THEN_CONFIRM\',\'RETEST_ONLY\'):\n        entry_mode=\'REGIME_STAGED\'\n\n    size_mult=float((regime_policy or {}).get(\'size_multiplier\') or 1.0)\n    if eff>=EXPERIENCE_MIN_SIZE_N and p is not None:\n        if float(p)>=0.72: size_mult*=1.20\n        elif float(p)>=0.64: size_mult*=1.10\n        elif float(p)<=0.38: size_mult*=0.55\n        elif float(p)<=0.46: size_mult*=0.75\n    if late_err>=0.25 and entry_state==\'LATE\':\n        size_mult=min(size_mult,0.60)\n    if entry_mode in (\'PULLBACK_OR_MIN_PROBE\',\'REGIME_STAGED\'):\n        size_mult=min(size_mult,0.70)\n    elif entry_mode in (\'RETEST_STAGED\',\'BREAKOUT_PROBE_THEN_CONFIRM\'):\n        size_mult=min(size_mult,0.85)\n    elif entry_mode==\'CONFIRMED_SCALE\' and p is not None and float(p)>=0.60:\n        size_mult=max(size_mult,1.05)\n    if rej>=0.65:\n        entry_mode=\'SIGNAL_FIRST_REINFORCED\'\n\n    stop_mult=float((regime_policy or {}).get(\'stop_multiplier\') or 1.0)\n    if eff>=EXPERIENCE_MIN_EXEC_N and stop_err>=0.25:\n        stop_mult*=min(1.20,1.0+0.45*(stop_err-0.20))\n    stop_mult=clip(stop_mult,0.90,V84_STOP_WIDEN_CAP)\n\n    # Exit optimization: protect against single-horizon noise. Hard invalidation remains immediate.\n    reg=(regime_policy or {}).get(\'regime_bucket\')\n    flip_ratio=1.20 if reg==\'TREND\' else 1.30 if reg in (\'HIGH_VOL_TREND\',\'TRANSITION\') else 1.15\n    flip_horizons=2\n    trail_activation_r=1.25 if reg==\'TREND\' else 1.50 if reg==\'HIGH_VOL_TREND\' else 0.90 if reg==\'RANGE\' else 1.10\n    return {\'version\':\'v84\',\'decision_influence\':True,\n            \'entry_mode\':entry_mode,\'size_multiplier\':round(clip(size_mult,0.50,1.30),4),\n            \'stop_distance_multiplier\':round(stop_mult,4),\n            \'add_allowed\':bool((regime_policy or {}).get(\'add_allowed\',True)),\n            \'flip_confirmation_ratio\':flip_ratio,\'flip_confirmation_horizons\':flip_horizons,\n            \'premature_exit_guard\':True,\'hard_invalidation_immediate\':True,\n            \'trail_mode\':\'STRUCTURAL_ONLY\',\'trail_activation_r\':trail_activation_r,\n            \'soft_deterioration_action\':\'HOLD_OR_REDUCE_NOT_EXIT\',\n            \'hard_gate_override\':False}\n\n\ndef apply_v84_execution_to_trade_plan(row,plan,memory,regime_policy,execution_policy):\n    plan=dict(plan or {})\n    plan[\'setup_memory\']=memory\n    plan[\'adaptive_regime_policy\']=regime_policy\n    plan[\'execution_policy\']=execution_policy\n\n    f0=float(plan.get(\'initial_position_fraction\') or 0.0)\n    if f0>0:\n        mult=float(execution_policy.get(\'size_multiplier\') or 1.0)\n        plan[\'initial_position_fraction\']=clip(max(0.05,f0*mult),0.05,1.0)\n    plan[\'experience_entry_mode\']=execution_policy.get(\'entry_mode\')\n\n    # Widen only when experience/regime evidence supports it; never tighten an established structural stop here.\n    sm=float(execution_policy.get(\'stop_distance_multiplier\') or 1.0)\n    stop=plan.get(\'stop_price\'); price=float((row or {}).get(\'price\') or 0.0)\n    if sm>1.0 and stop is not None and price>0 and direction_valid(row):\n        d=str((row or {}).get(\'research_decision\'))\n        try:\n            stop=float(stop); dist=abs(price-stop)\n            if dist>0:\n                nd=min(dist*sm,dist*V84_STOP_WIDEN_CAP)\n                candidate=price-nd if d==\'LONG\' else price+nd\n                if (d==\'LONG\' and candidate<stop) or (d==\'SHORT\' and candidate>stop):\n                    plan[\'experience_original_stop\']=stop\n                    plan[\'stop_price\']=candidate\n                    plan[\'stop_method\']=str(plan.get(\'stop_method\') or \'STRUCTURAL\')+\'+V84_BUFFER\'\n        except Exception:\n            pass\n\n    # Recompute economics honestly after any stop change.\n    try:\n        stop=float(plan.get(\'stop_price\')); price=float((row or {}).get(\'price\') or 0.0)\n        if price>0:\n            dist=abs(price-stop)/price\n            plan[\'stop_distance_pct\']=dist\n            exp=float(plan.get(\'expected_move_pct\') or 0.0)\n            plan[\'expected_to_stop_ratio\']=exp/dist if dist>1e-12 else 999.0\n    except Exception:\n        pass\n\n    plan[\'structural_stop_enforced\']=True\n    return plan\n\n\ndef direction_valid(row):\n    return str((row or {}).get(\'research_decision\') or \'\') in (\'LONG\',\'SHORT\')\n\n\ndef experience_profile_for_trade(asset,horizon,row,direction,plan,tradeability):\n    memory=setup_memory_profile(asset,horizon,row,direction,plan)\n    analog_n=int((tradeability or {}).get(\'raw_n\') or 0)\n    analog_eff=float((tradeability or {}).get(\'effective_n\') or 0.0)\n    analog_p=(tradeability or {}).get(\'positive_trade_probability\')\n    mp=memory.get(\'posterior_win_rate\'); me=float(memory.get(\'effective_n\') or 0.0)\n\n    weighted=[]\n    if analog_p is not None and analog_eff>0: weighted.append((float(analog_p),min(40.0,analog_eff)))\n    if mp is not None and me>0: weighted.append((float(mp),min(40.0,me)))\n    combined=sum(p*w for p,w in weighted)/sum(w for _,w in weighted) if weighted else None\n    effective=max(analog_eff,me)\n    level=\'OBSERVE\'\n    if effective>=EXPERIENCE_MIN_WEIGHT_N: level=\'WEIGHT_READY\'\n    elif effective>=EXPERIENCE_MIN_EXEC_N: level=\'EXECUTION_READY\'\n    elif effective>=EXPERIENCE_MIN_SIZE_N: level=\'SIZE_READY\'\n\n    return {\'status\':level,\'decision_influence\':level!=\'OBSERVE\',\n            \'asset\':asset,\'horizon\':horizon,\'direction\':direction,\n            \'combined_positive_probability\':None if combined is None else round(combined,4),\n            \'analog_raw_n\':analog_n,\'analog_effective_n\':round(analog_eff,2),\n            \'setup_memory_effective_n\':round(me,2),\'setup_memory\':memory,\n            \'hard_gate_override\':False}\n\n\ndef v84_direction_flip_confirmed(x,current_direction):\n    d=str((x or {}).get(\'research_decision\') or \'NO_TRADE\')\n    if d not in (\'LONG\',\'SHORT\') or d==current_direction:\n        return False\n    piv=(x or {}).get(\'impulse_pivot_break\') or {}\n    rev=(x or {}).get(\'tactical_reversal\') or {}\n    if piv.get(\'active\') and str(piv.get(\'direction\') or \'\')==d and float(piv.get(\'probability\') or 0)>=0.76:\n        return True\n    if rev.get(\'active\') and str(rev.get(\'direction\') or \'\')==d and float(rev.get(\'probability\') or 0)>=0.78:\n        return True\n    support=(x or {}).get(\'_uec_direction_support\') or {}\n    ours=float(support.get(d) or 0.0); other=float(support.get(current_direction) or 0.0)\n    hs=(x or {}).get(\'_uec_supporting_horizons\') or []\n    ep=((x or {}).get(\'trade_plan\') or {}).get(\'execution_policy\') or {}\n    ratio=float(ep.get(\'flip_confirmation_ratio\') or 1.20)\n    need=int(ep.get(\'flip_confirmation_horizons\') or 2)\n    return bool(len(hs)>=need and ours>=max(0.01,other)*ratio)\n\n\ndef execution_learning_board():\n    m=setup_memory_board()\n    items=m.get(\'items\') or []\n    exact=[x for x in items if x.get(\'level\')==\'EXACT\']\n    return {\'status\':m.get(\'status\'),\'exact_setups\':len(exact),\n            \'weight_ready\':sum(1 for x in exact if x.get(\'status\')==\'WEIGHT_READY\'),\n            \'execution_ready\':sum(1 for x in exact if x.get(\'status\') in (\'EXECUTION_READY\',\'WEIGHT_READY\')),\n            \'top_setups\':exact[:30],\n            \'principle\':\'direction, entry, stop and exit learn separately; hard safety gates never decay from experience\'}\n\n\ndef learning_index_v2():\n    if not pg_enabled(): return {\'status\':\'POSTGRES_REQUIRED\'}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT COALESCE(payload->>\'model_version\',\'UNKNOWN\') model_version,\n                                     COUNT(*) n,\n                                     COUNT(*) FILTER(WHERE profitable) wins,\n                                     COALESCE(AVG(net_pnl_rub),0) avg_pnl,\n                                     COALESCE(SUM(net_pnl_rub),0) total_pnl\n                              FROM paper_trades WHERE status=\'CLOSED\' GROUP BY 1""").fetchall()\n        by={str(r[\'model_version\']):dict(r) for r in rows}\n        def norm(x):\n            if not x: return {\'n\':0,\'wins\':0,\'win_rate\':None,\'avg_pnl\':None,\'total_pnl\':None}\n            n=int(x.get(\'n\') or 0); w=int(x.get(\'wins\') or 0)\n            return {\'n\':n,\'wins\':w,\'win_rate\':w/n if n else None,\n                    \'avg_pnl\':float(x.get(\'avg_pnl\') or 0.0),\'total_pnl\':float(x.get(\'total_pnl\') or 0.0)}\n        base=norm(by.get(\'veritas-portfolio-v6-v80-unified-execution\'))\n        cur=norm(by.get(\'veritas-portfolio-v8-v84-adaptive-execution\'))\n        measurable=cur[\'n\']>=20 and base[\'n\']>=8\n        gate=None\n        if measurable:\n            gate=bool(cur[\'win_rate\'] is not None and base[\'win_rate\'] is not None\n                      and cur[\'win_rate\']>=base[\'win_rate\'] and cur[\'avg_pnl\']>=base[\'avg_pnl\'])\n        return {\'status\':\'MEASURABLE\' if measurable else \'BUILDING\',\n                \'frozen_baseline\':\'veritas-portfolio-v6-v80-unified-execution\',\n                \'baseline\':base,\'v84\':cur,\'quality_gate_pass\':gate,\n                \'promotion_rule\':\'win rate must improve or hold while average P&L does not deteriorate\'}\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'error\':f\'{type(ex).__name__}: {ex}\'}\n'
PORTFOLIO_V84_HELPER="\ndef _candidate_book_v84(summary):\n    # Unified multi-timeframe portfolio routing with experience-aware ranking.\n    grouped={}\n    for r0 in summary or []:\n        r=dict(r0)\n        d=str(r.get('research_decision') or 'NO_TRADE')\n        tr=r.get('tactical_reversal') or {}\n        if tr.get('active') and tr.get('direction') in ('LONG','SHORT'):\n            d=str(tr.get('direction')); r['research_decision']=d\n        if d not in ('LONG','SHORT'): continue\n        if not bool(r.get('source_gate_pass',True)) or not bool(r.get('market_open',True)): continue\n        p,source=_signal_probability(r)\n        inst=r.get('institutional_signal') or {}; plan=r.get('trade_plan') or {}\n        ev=inst.get('evidence_independence') or {}; bq=inst.get('breakout_quality') or {}\n        indep=int(ev.get('independent_count') or 0); q=float(bq.get('quality_score') or 0.0)\n        rr=float(plan.get('expected_to_stop_ratio') or 0.0); tq=float(plan.get('trade_quality_score') or 0.0)\n        xp=plan.get('setup_memory') or {}; xpp=xp.get('posterior_win_rate')\n        bonus=0.0\n        if xpp is not None and float(xp.get('effective_n') or 0)>=8:\n            bonus=_clip((float(xpp)-0.50)*0.12,-0.03,0.03)\n        rank=float(p)+0.02*min(indep,6)+0.03*q+0.02*min(max(rr,0.0),2.0)+0.03*tq+bonus\n        r['_pwin']=p; r['_pwin_source']=source; r['_rank']=rank; r['_signal_first']=True\n        grouped.setdefault(str(r.get('asset')),[]).append(r)\n\n    out={}\n    for asset,rows in grouped.items():\n        support={'LONG':0.0,'SHORT':0.0}; hs={'LONG':[],'SHORT':[]}\n        for r in rows:\n            d=str(r.get('research_decision'))\n            support[d]+=max(0.01,float(r.get('_rank') or 0.0))\n            hs[d].append(str(r.get('horizon')))\n        chosen='LONG' if support['LONG']>=support['SHORT'] else 'SHORT'\n        eligible=[r for r in rows if str(r.get('research_decision'))==chosen]\n        if not eligible: continue\n        best=max(eligible,key=lambda r:float(r.get('_rank') or 0.0))\n        best=dict(best)\n        best['_direction_support']=support\n        best['_supporting_horizons']=sorted(set(hs[chosen]))\n        other='SHORT' if chosen=='LONG' else 'LONG'\n        ratio=float(support[chosen])/max(0.01,float(support[other]))\n        piv=best.get('impulse_pivot_break') or {}; rev=best.get('tactical_reversal') or {}\n        fast_confirm=bool(\n            (piv.get('active') and float(piv.get('probability') or 0)>=0.76) or\n            (rev.get('active') and float(rev.get('probability') or 0)>=0.78)\n        )\n        best['_flip_confirmed']=bool(\n            fast_confirm or (len(best['_supporting_horizons'])>=2 and ratio>=1.20)\n        )\n        out[asset]=best\n    return out\n"


def _read(path):
    return path.read_text(encoding="utf-8") if path.exists() else ""


def _write(path,text):
    path.write_text(text,encoding="utf-8")


def _already_v80():
    return V80_INTEL in _read(TARGET) and V80_PORT in _read(PORTFOLIO_TARGET)


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
                "VERSION = 'veritas-max-product-v84.0-adaptive-experience-execution'",1)
        elif 'VERSION = "veritas-max-product-v80.0-unified-execution-core"' in dst:
            dst=dst.replace(
                'VERSION = "veritas-max-product-v80.0-unified-execution-core"',
                'VERSION = "veritas-max-product-v84.0-adaptive-experience-execution"',1)
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

    old="""                plan=x.get('trade_plan') or {}
                stage=str(x.get('decision_stage') or tr.get('stage') or 'HOLD')
                desired=clip(float(plan.get('initial_position_fraction') or cur),0.05,1.0)

                # Scale only; soft deterioration no longer forces an independent shadow reduction.
                if desired>cur+1e-6 and stage in ('CONFIRMED_SCALE','CONFIRMED_FULL','ENTER_AND_SCALE','ENTER_FULL_CANDIDATE'):"""
    new="""                plan=x.get('trade_plan') or {}
                execp=plan.get('execution_policy') or setup_payload.get('execution_policy') or {}
                stage=str(x.get('decision_stage') or tr.get('stage') or 'HOLD')
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
        experience_seconds=time.time()-t
        dur=time.time()-started"""
    dst,ch=_replace_once(dst,old,new,"background experience learning")
    if ch: applied.append("background_learning")

    old="""                'event_learning':ev,'rule_learning':rr,'last_error':None,
                'runs':int(heavy_learning_state.get('runs') or 0)+1,
                'event_seconds':round(event_seconds,3),'rule_seconds':round(rule_seconds,3),'reason':reason"""
    new="""                'event_learning':ev,'rule_learning':rr,'experience_learning':xp,'last_error':None,
                'runs':int(heavy_learning_state.get('runs') or 0)+1,
                'event_seconds':round(event_seconds,3),'rule_seconds':round(rule_seconds,3),
                'experience_seconds':round(experience_seconds,3),'reason':reason"""
    dst,ch=_replace_once(dst,old,new,"background learning state")
    if ch: applied.append("learning_state")

    old="""             event_seconds=round(event_seconds,3),rule_seconds=round(rule_seconds,3),
             event_written=ev.get('written') if isinstance(ev,dict) else None,
             rule_rows=rr.get('rows') if isinstance(rr,dict) else None)"""
    new="""             event_seconds=round(event_seconds,3),rule_seconds=round(rule_seconds,3),
             experience_seconds=round(experience_seconds,3),
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
            "VERSION='veritas-portfolio-v8-v84-adaptive-execution'",1)
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

    old="""'canonical_setup_id':canonical_setup_id,"""
    new="""'canonical_setup_id':canonical_setup_id,
                 'experience_decision':plan.get('experience_decision'),
                 'setup_memory':plan.get('setup_memory'),
                 'adaptive_regime_policy':plan.get('adaptive_regime_policy'),
                 'execution_policy':plan.get('execution_policy'),"""
    dst,ch=_replace_once(dst,old,new,"persist v84 paper trade policy")
    if ch: applied.append("persist_policy")

    old="""'target_fraction':sf.get('fraction'),'reason':sf.get('reason')})"""
    new="""'target_fraction':sf.get('fraction'),'reason':sf.get('reason'),
                    'supporting_horizons':row.get('_supporting_horizons'),
                    'direction_support':row.get('_direction_support'),
                    'flip_confirmed':row.get('_flip_confirmed'),
                    'experience_decision':sf.get('experience_decision') or plan.get('execution_policy')})"""
    dst,ch=_replace_once(dst,old,new,"v84 admission trace")
    if ch: applied.append("admission_trace")

    old="""                           unified_execution=True)"""
    new="""                           unified_execution=True,experience_weighted=True,adaptive_regime=True,v84_execution=True)"""
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
        'learning_index':'def learning_index_v2()' in intel,
        'portfolio_candidate_book':'def _candidate_book_v84(summary):' in port,
        'portfolio_v84_routing':'candidates=_candidate_book_v84(summary)' in port,
        'portfolio_flip_guard':"V84_CONFIRMED_DIRECTION_FLIP" in port,
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
            print("[VERITAS BOOTSTRAP] v84 READY: idempotent=true; adaptive_experience_execution=true",flush=True)
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
            "[VERITAS BOOTSTRAP] v84 VERIFIED: "
            f"intelligence={','.join(ia)}; portfolio={','.join(pa)}; v70_sync={str(v70).lower()}",
            flush=True
        )
    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v84 FAILED: {type(exc).__name__}: {exc}",
            file=sys.stderr,flush=True
        )


_run()
