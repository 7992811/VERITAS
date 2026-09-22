"""
VERITAS v73.0 bootstrap patch.

Includes v72.1:
- dashboard timeout/refresh reliability
- horizon-aware invalidation
- closed-trade P&L in RUB + %

Includes v72.2:
- Breakout Capture Calibration for held breakouts
- local tactical breakout stop vs strategic thesis stop
- measured move remains active while breakout is held
- MOEX 50-point psychological breakout anchors

Adds v72.3:
- Brent REVERSAL_CAPTURE bridge:
  strong reversal candidate + old trend failure + volume + local range economics
  may open a 10-15% probe before slow regime fully flips.
- CNYRUBF promoted to explicitly calibrated FX-futures regime thresholds.
  It was already in the 35-cell universe and remains on all 5 horizons.
"""
from pathlib import Path
import sys

TARGET = Path(__file__).resolve().with_name("veritas_intelligence.py")

PATCHES = (
    # ---- v72.1 ----
    (
        "setTimeout(()=>ctl.abort(),12000)",
        "setTimeout(()=>ctl.abort(),30000)",
        "dashboard timeout",
    ),
    (
        "setInterval(load,30000)",
        "setInterval(load,45000)",
        "dashboard refresh",
    ),
    (
        "        elif str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='market_structure_failed'",
        "        elif str(x.get('v70_thesis_status') or '') in ('BROKEN','INVALIDATED') or str(x.get('v70_gate_class') or '')=='THESIS_VETO': terminal='INVALIDATION'; reason='thesis_failed'\n"
        "        elif h in ('1h','4h') and str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='tactical_entry_failed'",
        "horizon-aware invalidation",
    ),
    (
        """Net <b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}</b><br>${t.horizon||'—'}""",
        """Net <b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}${t.return_on_entry_nav==null?'':' · '+(100*Number(t.return_on_entry_nav)).toFixed(2)+'%'}</b><br>${t.horizon||'—'}""",
        "closed trade P&L percent",
    ),

    # ---- v72.2 breakout capture ----
    (
        """    add_candidate('STRUCTURAL_INVALIDATION',raw_inv)
    volstop=p*(1-max(0.0015,1.2*sigma)) if direction=='LONG' else p*(1+max(0.0015,1.2*sigma))""",
        """    add_candidate('STRUCTURAL_INVALIDATION',raw_inv)

    # v72.2 Breakout Capture Calibration.
    held_breakout=bool(
        st.get('breakout_hold') and not st.get('false_breakout') and
        float(st.get('score') or 0.0)>=0.65 and
        float(st.get('session_range_position') or 0.0)>=0.80 and
        float(st.get('session_persistence') or 0.0)>=0.60 and
        str(ti.get('phase') or '') in ('EARLY_TREND','TREND_DAY','IMPULSE_TREND')
    )
    tactical_breakout_anchor=None
    if held_breakout and breakout is not None:
        tactical_breakout_anchor=float(breakout)

    psych_anchor=None
    if held_breakout and asset=='MOEX' and p>0:
        step=50.0
        if direction=='LONG':
            lvl=(p//step)*step
            if lvl>0 and 0.0 <= (p-lvl)/p <= 0.0040:
                psych_anchor=lvl
        else:
            lvl=((p+step-1e-12)//step)*step
            if lvl>0 and 0.0 <= (lvl-p)/p <= 0.0040:
                psych_anchor=lvl
    if psych_anchor is not None:
        tactical_breakout_anchor=psych_anchor

    if tactical_breakout_anchor is not None:
        add_candidate('TACTICAL_BREAKOUT_LEVEL',tactical_breakout_anchor)

    volstop=p*(1-max(0.0015,1.2*sigma)) if direction=='LONG' else p*(1+max(0.0015,1.2*sigma))""",
        "v72.2 breakout tactical stop",
    ),
    (
        """    fresh=bool(st.get('fresh_breakout'))
    priority=(['RECENT_SWING_LOW_HIGH','CONFIRMED_PULLBACK_LOW_HIGH','STRUCTURAL_INVALIDATION','BREAKOUT_LEVEL','SESSION_EXTREME','VOLATILITY_FALLBACK']
              if fresh else ['CONFIRMED_PULLBACK_LOW_HIGH','BREAKOUT_LEVEL','STRUCTURAL_INVALIDATION','SESSION_EXTREME','VOLATILITY_FALLBACK'])""",
        """    fresh=bool(st.get('fresh_breakout'))
    if held_breakout and any(x.get('method')=='TACTICAL_BREAKOUT_LEVEL' for x in candidates):
        priority=['TACTICAL_BREAKOUT_LEVEL','RECENT_SWING_LOW_HIGH','BREAKOUT_LEVEL','CONFIRMED_PULLBACK_LOW_HIGH','STRUCTURAL_INVALIDATION','SESSION_EXTREME','VOLATILITY_FALLBACK']
    elif fresh:
        priority=['RECENT_SWING_LOW_HIGH','CONFIRMED_PULLBACK_LOW_HIGH','STRUCTURAL_INVALIDATION','BREAKOUT_LEVEL','SESSION_EXTREME','VOLATILITY_FALLBACK']
    else:
        priority=['CONFIRMED_PULLBACK_LOW_HIGH','BREAKOUT_LEVEL','STRUCTURAL_INVALIDATION','SESSION_EXTREME','VOLATILITY_FALLBACK']""",
        "v72.2 breakout stop priority",
    ),
    (
        """    stop_dist=abs(p-stop)/p if p else 999.0; exp=float(est.get('expected_move_pct') or 0.0)
    if fresh: exp=max(exp,float(st.get('breakout_measured_move_pct') or 0.0))
    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0""",
        """    stop_dist=abs(p-stop)/p if p else 999.0; exp=float(est.get('expected_move_pct') or 0.0)
    if fresh or held_breakout:
        exp=max(exp,float(st.get('breakout_measured_move_pct') or 0.0))
    ratio=exp/stop_dist if stop_dist>1e-12 else 999.0""",
        "v72.2 held breakout measured move",
    ),

    # ---- v72.3 Brent reversal capture ----
    (
        """                range_setup=range_retest_breakout_setup(asset,raw,f,institutional_signal)
                f['range_retest_breakout']=range_setup
                if range_setup.get('active') and research_dec==range_setup.get('direction') and horizon in ('1h','4h','1d'):""",
        """                range_setup=range_retest_breakout_setup(asset,raw,f,institutional_signal)
                f['range_retest_breakout']=range_setup

                # v72.3 Brent REVERSAL_CAPTURE.
                # Do not wait for every slow regime label to flip when the fast layer already
                # sees a strong opposite impulse, the old trend has failed, volume confirms,
                # and the local range supplies acceptable stop/target economics.
                reversal_capture=None
                if asset=='BRENT' and horizon in ('1h','4h','1d') and research_dec in ('LONG','SHORT'):
                    tr=tactical_reversal or {}
                    rs=range_setup or {}
                    tr_reasons=tr.get('reasons') or {}
                    candidate=str(tr.get('candidate_direction') or 'NO_TRADE')
                    rs_candidate=str(rs.get('candidate_direction') or 'NO_TRADE')
                    prob=float(tr.get('probability') or 0.0)
                    rr=float(rs.get('reward_risk') or 0.0)
                    impulse_ok=bool(tr_reasons.get('impulse'))
                    old_failed=bool(tr_reasons.get('old_trend_invalidated'))
                    volume_ok=bool(tr_reasons.get('volume')) or float(rs.get('volume_ratio_5m') or 0.0)>=1.20
                    local_econ_ok=bool(rr>=1.20 and rs.get('stop_price') is not None and rs.get('target_price') is not None)
                    alignment_ok=bool(candidate==research_dec and rs_candidate==research_dec)
                    if alignment_ok and prob>=0.72 and impulse_ok and old_failed and volume_ok and local_econ_ok:
                        reversal_capture={
                            'active':True,'direction':research_dec,'probability':round(prob,4),
                            'stop_price':rs.get('stop_price'),'target_price':rs.get('target_price'),
                            'reward_risk':rr,'setup':'BRENT_REVERSAL_CAPTURE',
                            'initial_position_fraction':0.15 if prob>=0.78 and rr>=1.40 else 0.10,
                            'evidence':{'impulse':impulse_ok,'old_trend_failed':old_failed,'volume':volume_ok,
                                        'local_rr':rr,'range_state':rs.get('state')}
                        }
                        trade_plan.update({
                            'eligible':True,'reason':'brent_reversal_capture',
                            'stop_price':reversal_capture['stop_price'],
                            'stop_method':'LOCAL_REVERSAL_RANGE_STOP',
                            'stop_distance_pct':abs(float(f.get('price') or 0)-float(reversal_capture['stop_price']))/max(float(f.get('price') or 1),1e-9),
                            'expected_move_pct':abs(float(reversal_capture['target_price'])/float(f.get('price') or 1)-1),
                            'expected_to_stop_ratio':rr,'min_expected_to_stop_ratio':1.20,
                            'initial_position_fraction':reversal_capture['initial_position_fraction'],
                            'scaling_policy':'REVERSAL_CAPTURE_10_15PCT_THEN_CONFIRM',
                            'tactical_target_price':reversal_capture['target_price'],
                            'setup':'BRENT_REVERSAL_CAPTURE',
                            'reversal_probability':prob
                        })
                        f['reversal_capture']=reversal_capture

                if range_setup.get('active') and research_dec==range_setup.get('direction') and horizon in ('1h','4h','1d'):""",
        "v72.3 Brent reversal capture",
    ),

    # ---- v72.3 CNYRUBF calibration ----
    (
        """      'NDX':(0.035,0.012,0.010),'MOEX':(0.055,0.015,0.012),
      'GOLD':(0.050,0.012,0.010),'BRENT':(0.070,0.020,0.015)}""",
        """      'NDX':(0.035,0.012,0.010),'MOEX':(0.055,0.015,0.012),
      'GOLD':(0.050,0.012,0.010),'BRENT':(0.070,0.020,0.015),
      'CNYRUBF':(0.025,0.006,0.005)}""",
        "v72.3 CNYRUBF regime calibration",
    ),

    # ----- v73.0 Early Impulse Intelligence -----
    (
        "\n\ndef impulse_breakdown_setup(asset, raw, f, causal_score=0.0):",
        """

def impulse_genesis_setup(asset, raw, f, causal_score=0.0):
    \"\"\"Earliest tradable impulse transition at a pre-existing local level.

    Detect the first meaningful break of a known local high/low and create a
    small probe before slow 1h/4h confirmation. Structural stop is anchored
    beyond the pre-impulse swing, not a random close-by bar inside the impulse.
    \"\"\"
    p=float(f.get('price') or raw.get('price') or 0.0)
    if p<=0:
        return {'active':False,'direction':'NO_TRADE','setup':'IMPULSE_GENESIS','reason':'no_price'}

    bars=list(raw.get('intraday_bars') or raw.get('intraday_5m') or [])
    if len(bars)<10:
        return {'active':False,'direction':'NO_TRADE','setup':'IMPULSE_GENESIS','reason':'insufficient_intraday_data'}

    z=bars[-48:]
    closes=[float(x.get('close') or 0.0) for x in z]
    highs=[float(x.get('high') or x.get('close') or 0.0) for x in z]
    lows=[float(x.get('low') or x.get('close') or 0.0) for x in z]
    vols=[float(x.get('volume') or 0.0) for x in z]
    if min(closes[-4:])<=0:
        return {'active':False,'direction':'NO_TRADE','setup':'IMPULSE_GENESIS','reason':'bad_price'}

    lev=f.get('structural_levels') or {}
    st=f.get('intraday_structure') or {}
    hs=f.get('horizon_structure') or {}

    structural_res=lev.get('resistance')
    structural_sup=lev.get('support')
    recent_res=_recent_swing_anchor(highs[:-1],lows[:-1],'SHORT',lookback=24)
    recent_sup=_recent_swing_anchor(highs[:-1],lows[:-1],'LONG',lookback=24)

    long_trigger=None
    short_trigger=None
    for x in (structural_res,recent_res):
        if x is not None and float(x)>0 and float(x)<=p*1.01:
            long_trigger=max(float(x), long_trigger or float(x))
    for x in (structural_sup,recent_sup):
        if x is not None and float(x)>0 and float(x)>=p*0.99:
            short_trigger=min(float(x), short_trigger or float(x))

    atr=max(_bar_atr([{'high':highs[i],'low':lows[i],'close':closes[i]} for i in range(len(closes))],14),p*0.0006)
    buf=max(0.04*atr,p*0.00020)

    prev=max(closes[-3:-1]) if len(closes)>=3 else closes[-2]
    prev_min=min(closes[-3:-1]) if len(closes)>=3 else closes[-2]
    long_break=bool(long_trigger is not None and prev<=long_trigger+buf and p>long_trigger+buf)
    short_break=bool(short_trigger is not None and prev_min>=short_trigger-buf and p<short_trigger-buf)

    r1=closes[-1]/closes[-2]-1
    r3=closes[-1]/closes[-4]-1 if len(closes)>=4 else r1
    hist=[closes[i]/closes[i-1]-1 for i in range(1,len(closes)-1) if closes[i-1]]
    sig=_robust_sigma(hist[-36:],0.0007)
    z3=r3/max(sig*math.sqrt(3.0),1e-9)

    lastv=sum(vols[-2:])/2.0 if len(vols)>=2 else vols[-1]
    basev=[x for x in vols[-14:-2] if x>0]
    med=_median_value(basev) if basev else 0.0
    vr=(lastv/med) if med>0 else None
    volume_ok=bool(vr is None or vr>=0.85)
    strong_volume=bool(vr is not None and vr>=1.20)

    k=min(6,len(closes)-1)
    rrseq=[closes[i]/closes[i-1]-1 for i in range(len(closes)-k,len(closes)) if closes[i-1]]
    path=sum(abs(x) for x in rrseq)
    net=closes[-1]/closes[-1-k]-1 if len(closes)>k and closes[-1-k] else r3
    eff=abs(net)/path if path>1e-12 else 0.0

    bar_range=max(highs[-1]-lows[-1],1e-9)
    close_pos=(closes[-1]-lows[-1])/bar_range

    if long_break:
        direction='LONG'
        anchors=[x for x in (recent_sup,structural_sup) if x is not None and float(x)<float(long_trigger)]
        if not anchors:
            anchors=[min(lows[-min(12,len(lows)):-1])]
        pre_impulse=float(max(anchors))
        stop=pre_impulse-max(0.10*atr,p*0.00035)
        risk=max(p-stop,p*0.0005)
        target=p+max(1.6*risk,abs(p-float(long_trigger))*2.0)
        impulse_ok=bool(r1>=0.0007 or r3>=0.0015 or z3>=0.85)
        close_ok=close_pos>=0.58
        native_ok=str(hs.get('direction') or '') in ('LONG','NO_TRADE')
        trigger=long_trigger
    elif short_break:
        direction='SHORT'
        anchors=[x for x in (recent_res,structural_res) if x is not None and float(x)>float(short_trigger)]
        if not anchors:
            anchors=[max(highs[-min(12,len(highs)):-1])]
        pre_impulse=float(min(anchors))
        stop=pre_impulse+max(0.10*atr,p*0.00035)
        risk=max(stop-p,p*0.0005)
        target=p-max(1.6*risk,abs(float(short_trigger)-p)*2.0)
        impulse_ok=bool(r1<=-0.0007 or r3<=-0.0015 or z3<=-0.85)
        close_ok=close_pos<=0.42
        native_ok=str(hs.get('direction') or '') in ('SHORT','NO_TRADE')
        trigger=short_trigger
    else:
        cand='LONG' if long_trigger is not None and p>=long_trigger*(1-0.0015) else 'SHORT' if short_trigger is not None and p<=short_trigger*(1+0.0015) else 'NO_TRADE'
        return {'active':False,'direction':'NO_TRADE','candidate_direction':cand,'setup':'IMPULSE_GENESIS',
                'reason':'waiting_for_level_break','long_trigger':long_trigger,'short_trigger':short_trigger,
                'local_volume_ratio':vr,'z3':round(z3,3)}

    stop_dist=abs(p-stop)/p
    reward=abs(target-p)/p
    rr=reward/max(stop_dist,1e-9)
    old_invalid=str((f.get('trend_impulse') or {}).get('entry_quality') or '')=='INVALIDATED' or str(st.get('lifecycle') or '')=='FAILURE'
    confirmations=sum([impulse_ok,volume_ok,eff>=0.35,close_ok,native_ok,old_invalid or strong_volume])

    prob=0.58
    prob += 0.08 if impulse_ok else 0.0
    prob += 0.05 if volume_ok else 0.0
    prob += 0.04 if strong_volume else 0.0
    prob += 0.05 if eff>=0.35 else 0.0
    prob += 0.04 if close_ok else 0.0
    prob += 0.035 if native_ok else 0.0
    prob += 0.025 if old_invalid else 0.0
    prob=clip(prob,0.50,0.90)

    active=bool(confirmations>=4 and prob>=0.68 and rr>=0.75)
    initial=0.20 if prob>=0.78 and rr>=1.20 else 0.15 if prob>=0.72 else 0.10

    return {'active':active,'direction':direction if active else 'NO_TRADE','candidate_direction':direction,
            'setup':'IMPULSE_GENESIS','reason':'early_local_level_break' if active else 'genesis_quality_gate',
            'probability':round(prob,4),'confirmations':confirmations,
            'trigger_level':trigger,'pre_impulse_swing':pre_impulse,
            'stop_price':stop,'target_price':target,'reward_risk':round(rr,3),
            'initial_position_fraction':initial,'local_volume_ratio':vr,
            'local_efficiency':round(eff,4),'z3':round(z3,3),
            'structural_stop_policy':'PRE_IMPULSE_SWING',
            'evidence':{'impulse':impulse_ok,'volume':volume_ok,'strong_volume':strong_volume,
                        'path_efficiency':eff>=0.35,'close_location':close_ok,
                        'native_not_opposed':native_ok,'old_structure_failed':old_invalid}}


def impulse_breakdown_setup(asset, raw, f, causal_score=0.0):""",
        "v73.0 impulse genesis engine",
    ),
    (
        """                tactical_reversal=tactical_reversal_features(asset,f,_prev_prices.get(asset),causal_shadow.get('score'))
                pivot_break=impulse_breakdown_setup(asset,raw,f,causal_shadow.get('score'))""",
        """                tactical_reversal=tactical_reversal_features(asset,f,_prev_prices.get(asset),causal_shadow.get('score'))
                impulse_genesis=impulse_genesis_setup(asset,raw,f,causal_shadow.get('score'))
                pivot_break=impulse_breakdown_setup(asset,raw,f,causal_shadow.get('score'))

                if impulse_genesis.get('active') and (not tactical_reversal.get('active') or float(impulse_genesis.get('probability') or 0)>=float(tactical_reversal.get('probability') or 0)-0.03):
                    tactical_reversal=impulse_genesis
                f['impulse_genesis']=impulse_genesis""",
        "v73.0 genesis integration",
    ),
    (
        """                if pivot_break.get('active') and (not tactical_reversal.get('active') or float(pivot_break.get('probability') or 0)>=float(tactical_reversal.get('probability') or 0)):
                    tactical_reversal=pivot_break""",
        """                if pivot_break.get('active') and (not tactical_reversal.get('active') or float(pivot_break.get('probability') or 0)>=float(tactical_reversal.get('probability') or 0)+0.02):
                    tactical_reversal=pivot_break""",
        "v73.0 staged impulse arbitration",
    ),
    (
        """                if tactical_reversal.get('active') and research_dec==tactical_reversal.get('direction'):
                    trade_plan.update({'eligible':True,'reason':'tactical_reversal','stop_price':tactical_reversal.get('stop_price'),
                                       'expected_move_pct':abs(float(tactical_reversal.get('target_price') or f.get('price'))/float(f.get('price') or 1)-1),
                                       'expected_to_stop_ratio':tactical_reversal.get('reward_risk'),'min_expected_to_stop_ratio':1.30,
                                       'initial_position_fraction':min(0.15,float(size or 0.05)),'scaling_policy':'TACTICAL_REVERSAL_5_15PCT',
                                       'tactical_target_price':tactical_reversal.get('target_price'),'setup':tactical_reversal.get('setup') or 'TACTICAL_REVERSAL',
                                       'reversal_probability':tactical_reversal.get('probability')})""",
        """                if tactical_reversal.get('active') and research_dec==tactical_reversal.get('direction'):
                    is_genesis=str(tactical_reversal.get('setup') or '')=='IMPULSE_GENESIS'
                    trade_plan.update({'eligible':True,'reason':'impulse_genesis' if is_genesis else 'tactical_reversal',
                                       'stop_price':tactical_reversal.get('stop_price'),
                                       'stop_method':'PRE_IMPULSE_SWING' if is_genesis else 'TACTICAL_REVERSAL_STRUCTURE',
                                       'expected_move_pct':abs(float(tactical_reversal.get('target_price') or f.get('price'))/float(f.get('price') or 1)-1),
                                       'expected_to_stop_ratio':tactical_reversal.get('reward_risk'),
                                       'min_expected_to_stop_ratio':0.75 if is_genesis else 1.30,
                                       'initial_position_fraction':tactical_reversal.get('initial_position_fraction',min(0.15,float(size or 0.05))),
                                       'scaling_policy':'IMPULSE_GENESIS_PROBE_THEN_ADD' if is_genesis else 'TACTICAL_REVERSAL_5_15PCT',
                                       'tactical_target_price':tactical_reversal.get('target_price'),
                                       'setup':tactical_reversal.get('setup') or 'TACTICAL_REVERSAL',
                                       'pre_impulse_swing':tactical_reversal.get('pre_impulse_swing'),
                                       'trigger_level':tactical_reversal.get('trigger_level'),
                                       'reversal_probability':tactical_reversal.get('probability')})""",
        "v73.0 structural stop hierarchy",
    ),
    (
        """                f['impulse_pivot_break']=pivot_break
                f['tactical_reversal']=tactical_reversal""",
        """                f['impulse_pivot_break']=pivot_break
                f['tactical_reversal']=tactical_reversal
                if horizon in ('1h','4h') and (impulse_genesis.get('candidate_direction') in ('LONG','SHORT') or impulse_genesis.get('active')):
                    try:
                        pg_event('impulse_genesis_learning',f'{cycle_id}:{asset}:{horizon}:genesis',
                                 {'setup':'IMPULSE_GENESIS','active':bool(impulse_genesis.get('active')),
                                  'candidate_direction':impulse_genesis.get('candidate_direction'),
                                  'price':f.get('price'),'trigger_level':impulse_genesis.get('trigger_level'),
                                  'pre_impulse_swing':impulse_genesis.get('pre_impulse_swing'),
                                  'stop_price':impulse_genesis.get('stop_price'),
                                  'target_price':impulse_genesis.get('target_price'),
                                  'reward_risk':impulse_genesis.get('reward_risk'),
                                  'probability':impulse_genesis.get('probability'),
                                  'counterfactual_entry_price':f.get('price'),
                                  'lesson':'detect impulse at first meaningful local high/low break; entry timing and structural stop are separate decisions'},
                                 asset,horizon,created_at)
                    except Exception as ex:
                        emit('impulse_genesis_learning_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')""",
        "v73.0 missed move forensics and counterfactual replay",
    ),
)

def apply():
    try:
        if not TARGET.exists():
            print("[VERITAS BOOTSTRAP] main file missing", file=sys.stderr, flush=True)
            return

        src = TARGET.read_text(encoding="utf-8")
        dst = src
        applied, already = [], []

        for old, new, label in PATCHES:
            if old in dst:
                dst = dst.replace(old, new, 1)
                applied.append(label)
            elif new in dst:
                already.append(label)
            else:
                raise RuntimeError(f"expected pattern not found: {label}")

        if dst != src:
            TARGET.write_text(dst, encoding="utf-8")

        print(
            "[VERITAS BOOTSTRAP] v73.0 OK: "
            + "; ".join(applied + [x + " (already)" for x in already]),
            flush=True,
        )
    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v72.3 skipped safely: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

apply()
