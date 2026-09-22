"""
VERITAS v76.0 bootstrap patch.

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

    # ----- v74.0 runtime identity -----
    (
        "VERSION = 'veritas-max-product-v70.8.4-range-participation'",
        "VERSION = 'veritas-max-product-v74.0-profitability-closed-loop'",
        "v74.0 runtime version",
    ),

    # ----- v74.0 true 5m crypto data -----
    (
        """def market(symbol, coinbase_product):
    k = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 240})""",
        """def market(symbol, coinbase_product):
    k = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '1h', 'limit': 240})
    k5 = get_json('https://api.binance.com/api/v3/klines', {'symbol': symbol, 'interval': '5m', 'limit': 288})""",
        "v74.0 crypto 5m fetch",
    ),
    (
        """        'taker_buy': taker_buy, 'returns': rets, 'binance_close_time_ms': close_time_ms,
        'observed_at': obs,'source_gate_pass':True,'market_open':True,'source_quality':quality
    }""",
        """        'taker_buy': taker_buy, 'returns': rets, 'binance_close_time_ms': close_time_ms,
        'intraday_bars':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),
                          'close':float(x[4]),'volume':float(x[5])} for x in k5[-288:]],
        'intraday_5m':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),
                       'close':float(x[4]),'volume':float(x[5])} for x in k5[-288:]],
        'observed_at': obs,'source_gate_pass':True,'market_open':True,'source_quality':quality
    }""",
        "v74.0 crypto 5m raw",
    ),

    # ----- v74.0 true 5m CNYRUBF data -----
    (
        """def _cnyrubf_market():
    end=time.time(); hist=_moex_futures_candles_between('CNYRUBF',end-120*86400,end+86400,60)
    if len(hist)<120: raise RuntimeError(f'INSUFFICIENT_CNYRUBF_HOURLY_BARS {len(hist)}')""",
        """def _cnyrubf_market():
    end=time.time(); hist=_moex_futures_candles_between('CNYRUBF',end-120*86400,end+86400,60)
    intr5=_moex_futures_candles_between('CNYRUBF',end-5*86400,end+86400,5)
    if len(hist)<120: raise RuntimeError(f'INSUFFICIENT_CNYRUBF_HOURLY_BARS {len(hist)}')""",
        "v74.0 CNYRUBF 5m fetch",
    ),
    (
        """            'data_latency_class':'DELAYED_RESEARCH','verification_mode':'single_direct_official',
            'source_names':{'primary':'MOEX ISS CNYRUBF','secondary':'NOT_CONFIGURED'},""",
        """            'data_latency_class':'DELAYED_RESEARCH','verification_mode':'single_direct_official',
            'intraday_bars':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),
                              'close':float(x[4]),'volume':float(x[5])} for x in intr5[-288:]],
            'intraday_5m':[{'ts':int(x[0])//1000,'open':float(x[1]),'high':float(x[2]),'low':float(x[3]),
                           'close':float(x[4]),'volume':float(x[5])} for x in intr5[-288:]],
            'source_names':{'primary':'MOEX ISS CNYRUBF','secondary':'NOT_CONFIGURED'},""",
        "v74.0 CNYRUBF 5m raw",
    ),

    # ----- v74.0 profitability memory + gate -----
    (
        "\n\ndef technical_trade_plan(asset,horizon,f,research_decision,signal_tier,analog=None):",
        """

def setup_profitability_profile(asset,horizon,direction,setup_name,regime=None,limit=240):
    \"\"\"Closed-loop profitability memory for comparable completed shadow trades.

    Uses only completed trades. Sparse samples never block a trade. The gate becomes
    influential only after enough independent closed observations exist.
    \"\"\"
    if not pg_enabled() or direction not in ('LONG','SHORT'):
        return {'status':'BUILDING','n':0,'decision_influence':False}
    try:
        with pg_connect() as c:
            rows=c.execute(\"\"\"SELECT asset,horizon,direction,total_pnl_fraction,payload
                              FROM shadow_trades
                              WHERE status<>'ACTIVE' AND total_pnl_fraction IS NOT NULL
                                AND asset=%s AND horizon=%s AND direction=%s
                              ORDER BY closed_at DESC NULLS LAST LIMIT %s\"\"\",
                           (asset,horizon,direction,int(limit))).fetchall()
        vals=[]; matched=0
        for rr in rows:
            x=dict(rr); pay=x.get('payload')
            if not isinstance(pay,dict):
                try: pay=json.loads(pay or '{}')
                except Exception: pay={}
            psetup=str(pay.get('setup') or pay.get('trade_plan_setup') or pay.get('entry_setup') or '')
            pregime=str(pay.get('regime') or '')
            if setup_name and psetup and psetup!=setup_name:
                continue
            # Regime is a soft match: use exact regime where available, but don't discard old
            # samples that predate regime tagging.
            if regime and pregime and pregime!=str(regime):
                continue
            vals.append(float(x.get('total_pnl_fraction') or 0.0)); matched+=1

        n=len(vals)
        if not n:
            return {'status':'BUILDING','n':0,'decision_influence':False}
        wins=[v for v in vals if v>0]; losses=[v for v in vals if v<0]
        avg=sum(vals)/n; hit=len(wins)/n
        gross_win=sum(wins); gross_loss=abs(sum(losses))
        pf=(gross_win/gross_loss) if gross_loss>1e-12 else (9.99 if gross_win>0 else 0.0)
        # Bayesian shrinkage for win rate prevents small-sample overreaction.
        post_hit=(len(wins)+3.0)/(n+6.0)
        measurable=n>=8
        strong=n>=12 and avg>0 and hit>=0.55 and pf>=1.20
        weak=n>=12 and avg<0 and hit<=0.42 and pf<0.85
        degraded=n>=8 and avg<0 and post_hit<0.48
        status='POSITIVE_EDGE' if strong else 'NEGATIVE_EDGE' if weak else 'DEGRADED' if degraded else 'BUILDING'
        return {'status':status,'n':n,'avg_pnl_fraction':avg,'hit_rate':hit,'posterior_hit_rate':post_hit,
                'profit_factor':pf,'decision_influence':bool(measurable),
                'setup':setup_name,'regime':regime}
    except Exception as ex:
        return {'status':'ERROR','n':0,'decision_influence':False,'error':f'{type(ex).__name__}: {ex}'}


def profitability_gate(asset,horizon,direction,f,trade_plan):
    \"\"\"Use realized setup economics to size/suppress recurrence of losing patterns.

    Sparse history never blocks. A hard block requires >=12 closed comparable trades,
    negative mean P&L, low hit rate and profit factor below 0.85 simultaneously.
    \"\"\"
    plan=trade_plan or {}
    if direction not in ('LONG','SHORT') or not plan.get('eligible'):
        return {'status':'NOT_APPLICABLE','allow':bool(plan.get('eligible')),'size_multiplier':1.0,'profile':{}}
    setup_name=str(plan.get('setup') or plan.get('reason') or 'GENERIC')
    profile=setup_profitability_profile(asset,horizon,direction,setup_name,f.get('regime'))
    status=profile.get('status')
    mult=1.0; allow=True
    if status=='NEGATIVE_EDGE':
        mult=0.0; allow=False
    elif status=='DEGRADED':
        mult=0.50
    elif status=='POSITIVE_EDGE':
        mult=1.15
    # Never let this layer increase an initial fraction above 100%.
    return {'status':status,'allow':allow,'size_multiplier':mult,'profile':profile,
            'policy':'closed-trade setup memory; sparse samples cannot veto; confirmed negative edge can suppress recurrence'}


def technical_trade_plan(asset,horizon,f,research_decision,signal_tier,analog=None):""",
        "v74.0 profitability memory and gate",
    ),

    # Apply gate after all tactical/reversal/range plan overrides, before plan persistence.
    (
        """                if institutional_signal.get('signal_tier') in ('SUPER_LONG','SUPER_SHORT'):
                    research_signal_tier=institutional_signal.get('signal_tier')
                    signal_tier=research_signal_tier
                    execution_signal_tier=research_signal_tier if execution_gate.get('eligible') else 'NO_TRADE'
                trade_plan['institutional_signal']=institutional_signal""",
        """                if institutional_signal.get('signal_tier') in ('SUPER_LONG','SUPER_SHORT'):
                    research_signal_tier=institutional_signal.get('signal_tier')
                    signal_tier=research_signal_tier
                    execution_signal_tier=research_signal_tier if execution_gate.get('eligible') else 'NO_TRADE'

                # v74 closed-loop profitability gate. It acts only on sufficient CLOSED-trade
                # evidence and never penalizes a new setup merely because history is sparse.
                profit_gate=profitability_gate(asset,horizon,research_dec,f,trade_plan)
                trade_plan['profitability_gate']=profit_gate
                pm=float(profit_gate.get('size_multiplier') or 0.0)
                if trade_plan.get('eligible') and not profit_gate.get('allow',True):
                    trade_plan['eligible']=False
                    trade_plan['reason']='historically_negative_setup_edge'
                    trade_plan['initial_position_fraction']=0.0
                elif trade_plan.get('eligible') and pm!=1.0:
                    trade_plan['initial_position_fraction']=clip(float(trade_plan.get('initial_position_fraction') or 0.0)*pm,0.0,1.0)
                    trade_plan['scaling_policy']=str(trade_plan.get('scaling_policy') or '')+'|PROFITABILITY_'+str(profit_gate.get('status') or 'BUILDING')
                try:
                    if profit_gate.get('status') in ('NEGATIVE_EDGE','DEGRADED','POSITIVE_EDGE'):
                        pg_event('profitability_learning',f'{cycle_id}:{asset}:{horizon}:profitability',
                                 {'direction':research_dec,'price':f.get('price'),
                                  'setup':trade_plan.get('setup') or trade_plan.get('reason'),
                                  'regime':f.get('regime'),'gate':profit_gate,
                                  'lesson':'position size and recurrence are conditioned on realized closed-trade expectancy, not confidence alone'},
                                 asset,horizon,created_at)
                except Exception as ex:
                    emit('profitability_learning_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')
                trade_plan['institutional_signal']=institutional_signal""",
        "v74.0 profitability gate integration",
    ),

    # ----- v74.0 stop/entry counterfactual learning -----
    (
        """                trade_plan['positive_trade_probability']=tradeability.get('positive_trade_probability')
                trade_plan['statistical_noise_buffer_p80']=tradeability.get('p80_adverse_excursion')
                entity_key = f'{cycle_id}:{asset}:{horizon}'""",
        """                trade_plan['positive_trade_probability']=tradeability.get('positive_trade_probability')
                trade_plan['statistical_noise_buffer_p80']=tradeability.get('p80_adverse_excursion')

                # v74 counterfactual lab: persist candidate stop hierarchy and early-entry
                # anchors so outcomes can later compare which variant would have survived
                # and captured more MFE. This is diagnostic until sufficient samples exist.
                try:
                    if research_dec in ('LONG','SHORT'):
                        pg_event('trade_counterfactual_lab',f'{cycle_id}:{asset}:{horizon}:cf',
                                 {'direction':research_dec,'entry_price':f.get('price'),
                                  'setup':trade_plan.get('setup') or trade_plan.get('reason'),
                                  'chosen_stop':trade_plan.get('stop_price'),
                                  'chosen_stop_method':trade_plan.get('stop_method'),
                                  'stop_candidates':trade_plan.get('stop_candidates') or [],
                                  'pre_impulse_swing':trade_plan.get('pre_impulse_swing'),
                                  'trigger_level':trade_plan.get('trigger_level'),
                                  'expected_move_pct':trade_plan.get('expected_move_pct'),
                                  'reward_risk':trade_plan.get('expected_to_stop_ratio'),
                                  'profitability_gate':trade_plan.get('profitability_gate'),
                                  'lesson':'compare entry timing and stop variants against realized path; do not equate late entry with close stop'},
                                 asset,horizon,created_at)
                except Exception as ex:
                    emit('counterfactual_lab_error',asset=asset,horizon=horizon,error=f'{type(ex).__name__}: {ex}')
                entity_key = f'{cycle_id}:{asset}:{horizon}'""",
        "v74.0 stop entry counterfactual lab",
    ),


    # ----- v75.0 cumulative walk-forward profitability lab -----
    ("VERSION = 'veritas-max-product-v74.0-profitability-closed-loop'", "VERSION = 'veritas-max-product-v75.0-walkforward-profitability-lab'", 'v75.0 runtime version'),
    ("BACKTEST_METHOD_VERSION = 'v60_vault_timeblocks_costgrid_isotonic'", "BACKTEST_METHOD_VERSION = 'v75_walkforward_vault_stability_costs_profitability'", 'v75.0 backtest method version'),
    ("EXPECTED_EDGE_MIN_N = max(20, int(os.getenv('VERITAS_EXPECTED_EDGE_MIN_N','30')))", "EXPECTED_EDGE_MIN_N = max(20, int(os.getenv('VERITAS_EXPECTED_EDGE_MIN_N','30')))\n\n# v75 Walk-Forward Profitability Lab\nV75_CONF_GRID=(0.10,0.15,0.20,0.25,0.30,0.35)\nV75_ONSET_GRID=(0.00,0.20,0.35,0.50,0.65)\nV75_STRUCTURE_GRID=(0.00,0.20,0.35,0.50,0.65)\nV75_EFFICIENCY_GRID=(0.00,0.20,0.35,0.50)\nV75_VOLUME_GRID=(0.00,0.75,0.90,1.05,1.20)\nV75_MIN_OOS_N=max(12,int(os.getenv('VERITAS_V75_MIN_OOS_N','20')))\nV75_MIN_VAULT_N=max(8,int(os.getenv('VERITAS_V75_MIN_VAULT_N','12')))\nV75_MIN_PF=max(1.0,float(os.getenv('VERITAS_V75_MIN_PF','1.05')))\nV75_MIN_VAULT_PF=max(1.0,float(os.getenv('VERITAS_V75_MIN_VAULT_PF','1.02')))\nV75_PARAMETER_LAB_CACHE_SECONDS=max(60,int(os.getenv('VERITAS_V75_PARAMETER_LAB_CACHE_SECONDS','300')))", 'v75.0 parameter grids'),
    ('\n\ndef tradeability_analog_stats(asset,horizon,f,direction):', "\n\n\ndef _v75_trade_metrics(vals):\n    a=[float(x) for x in vals if x is not None and math.isfinite(float(x))]\n    n=len(a)\n    if not n:\n        return {'n':0,'hit_rate':None,'avg_return':None,'profit_factor':None,'max_drawdown':None,'score':None}\n    wins=[x for x in a if x>0]; losses=[x for x in a if x<0]\n    hit=len(wins)/n; avg=sum(a)/n\n    gw=sum(wins); gl=abs(sum(losses))\n    pf=(gw/gl) if gl>1e-12 else (9.99 if gw>0 else 0.0)\n    eq=peak=maxdd=0.0\n    for r in a:\n        eq+=r; peak=max(peak,eq); maxdd=max(maxdd,peak-eq)\n    score=(avg*10000.0)+8.0*math.log(max(0.20,pf))+12.0*(hit-0.50)-80.0*maxdd\n    return {'n':n,'hit_rate':hit,'avg_return':avg,'profit_factor':pf,\n            'max_drawdown':maxdd,'total_return_arithmetic':sum(a),'score':score}\n\ndef _v75_param_pass(r,param):\n    if str(r.get('research_decision') or '') not in ('LONG','SHORT'): return False\n    if float(r.get('confidence') or 0.0)<param['confidence_min']: return False\n    if float(r.get('onset_score') or 0.0)<param['onset_min']: return False\n    if float(r.get('structure_score') or 0.0)<param['structure_min']: return False\n    if float(r.get('session_efficiency') or 0.0)<param['efficiency_min']: return False\n    if param['volume_min']>0 and float(r.get('relative_volume') or 0.0)<param['volume_min']: return False\n    return True\n\ndef _v75_signed_net(r):\n    fr=r.get('forward_return'); d=str(r.get('research_decision') or '')\n    if fr is None or d not in ('LONG','SHORT'): return None\n    gross=float(fr) if d=='LONG' else -float(fr)\n    return gross-BACKTEST_COST_BPS/10000.0\n\ndef v75_parameter_lab(asset=None,horizon=None,force=False):\n    cache=getattr(v75_parameter_lab,'_cache',None); key=(asset,horizon)\n    if cache and not force and time.time()-cache[0]<V75_PARAMETER_LAB_CACHE_SECONDS and cache[1]==key:\n        return cache[2]\n\n    rows=list(reversed(_decision_memory_rows(force)))\n    if asset: rows=[x for x in rows if x.get('asset')==asset]\n    if horizon: rows=[x for x in rows if x.get('horizon')==horizon]\n    rows=[x for x in rows if x.get('forward_return') is not None and str(x.get('research_decision') or '') in ('LONG','SHORT')]\n    n=len(rows)\n    if n<40:\n        out={'status':'BUILDING','n':n,'asset':asset,'horizon':horizon,\n             'reason':'need_at_least_40_completed_directional_episodes',\n             'vault_used_for_selection':False,'automatic_champion_promotion':False}\n        v75_parameter_lab._cache=(time.time(),key,out); return out\n\n    i1=max(1,int(n*0.55)); i2=max(i1+1,int(n*0.85))\n    IS,OOS,VAULT=rows[:i1],rows[i1:i2],rows[i2:]\n    base_oos=_v75_trade_metrics([_v75_signed_net(x) for x in OOS])\n    base_vault=_v75_trade_metrics([_v75_signed_net(x) for x in VAULT])\n\n    grid=[]\n    for conf in V75_CONF_GRID:\n      for onset in V75_ONSET_GRID:\n       for struct in V75_STRUCTURE_GRID:\n        for eff in V75_EFFICIENCY_GRID:\n         for vol in V75_VOLUME_GRID:\n            q={'confidence_min':conf,'onset_min':onset,'structure_min':struct,'efficiency_min':eff,'volume_min':vol}\n            im=_v75_trade_metrics([_v75_signed_net(x) for x in IS if _v75_param_pass(x,q)])\n            om=_v75_trade_metrics([_v75_signed_net(x) for x in OOS if _v75_param_pass(x,q)])\n            if im['n']<20 or om['n']<V75_MIN_OOS_N: continue\n            if (im.get('avg_return') or 0)<=0 or (om.get('avg_return') or 0)<=0: continue\n            grid.append({'params':q,'is':im,'oos':om})\n\n    grid.sort(key=lambda x:(float(x['oos'].get('score') or -1e9),float(x['is'].get('score') or -1e9)),reverse=True)\n    selected=dict(grid[0]) if grid else None\n\n    if selected:\n        q=selected['params']\n        vm=_v75_trade_metrics([_v75_signed_net(x) for x in VAULT if _v75_param_pass(x,q)])\n        selected['vault']=vm\n        families={'confidence_min':V75_CONF_GRID,'onset_min':V75_ONSET_GRID,'structure_min':V75_STRUCTURE_GRID,\n                  'efficiency_min':V75_EFFICIENCY_GRID,'volume_min':V75_VOLUME_GRID}\n        neighbors=[]\n        for name,vals in families.items():\n            idx=vals.index(q[name])\n            for j in (idx-1,idx+1):\n                if 0<=j<len(vals):\n                    qq=dict(q); qq[name]=vals[j]\n                    m=_v75_trade_metrics([_v75_signed_net(x) for x in OOS if _v75_param_pass(x,qq)])\n                    if m['n']>=V75_MIN_OOS_N: neighbors.append({'changed':name,'value':vals[j],'oos':m})\n        good=sum(1 for z in neighbors if (z['oos'].get('avg_return') or 0)>0 and (z['oos'].get('profit_factor') or 0)>=1.0)\n        stability=good/len(neighbors) if neighbors else 0.0\n        om=selected['oos']\n        oos_pass=bool(om['n']>=V75_MIN_OOS_N and (om.get('avg_return') or 0)>0 and (om.get('profit_factor') or 0)>=V75_MIN_PF)\n        vault_pass=bool(vm['n']>=V75_MIN_VAULT_N and (vm.get('avg_return') or 0)>0 and (vm.get('profit_factor') or 0)>=V75_MIN_VAULT_PF)\n        improved=bool((om.get('avg_return') or -999)>(base_oos.get('avg_return') or -999) and\n                      (om.get('profit_factor') or 0)>=(base_oos.get('profit_factor') or 0))\n        selected['stability']={'neighbor_pass_share':stability,'neighbors':neighbors}\n        selected['oos_pass']=oos_pass; selected['vault_pass']=vault_pass; selected['baseline_improved']=improved\n        selected['promotion_status']='ROBUST_CHALLENGER' if (oos_pass and vault_pass and improved and stability>=0.60) else 'RESEARCH_ONLY'\n\n    out={'status':'MEASURABLE' if selected else 'NO_ROBUST_CANDIDATE','asset':asset,'horizon':horizon,'n':n,\n         'split':{'is_n':len(IS),'oos_n':len(OOS),'vault_n':len(VAULT),'is_share':0.55,'oos_share':0.30,'vault_share':0.15},\n         'baseline':{'oos':base_oos,'vault':base_vault},'selected':selected,'top_candidates':grid[:10],\n         'vault_used_for_selection':False,'automatic_champion_promotion':False,\n         'definition':'Candidate selected on chronological IS/OOS only; untouched VAULT is acceptance test; local neighbor stability required; costs deducted.',\n         'limitation':'Optimizes admission filters among historical directional decisions; missed NO_TRADE signals require the separate historical rule backtest.'}\n    v75_parameter_lab._cache=(time.time(),key,out); return out\n\ndef v75_asset_parameter_board():\n    items=[]\n    for asset in DISPLAY_ASSETS:\n        for h in HORIZONS:\n            z=v75_parameter_lab(asset,h); s=z.get('selected') or {}\n            if z.get('status')=='MEASURABLE' or s:\n                items.append({'asset':asset,'horizon':h,'status':z.get('status'),'promotion_status':s.get('promotion_status'),\n                              'params':s.get('params'),'oos':s.get('oos'),'vault':s.get('vault'),\n                              'stability':s.get('stability'),'baseline':z.get('baseline')})\n    return {'version':VERSION,'items':items,\n            'promotion_policy':'Only ROBUST_CHALLENGER may be considered for champion promotion; no automatic parameter rewrite.'}\n\ndef tradeability_analog_stats(asset,horizon,f,direction):", 'v75.0 walkforward parameter lab'),
    ("def validation_stack():\n    return {'validation':oos_validation_board(200),'time_stability':timeblock_stability_board(200),\n            'cost_sensitivity':cost_sensitivity_board(200),'calibration_quality':calibration_quality(),\n            'expected_edge':expected_edge_map(),'signal_readiness':signal_readiness_report(),'portfolio_stress':portfolio_stress()}", "def validation_stack():\n    return {'validation':oos_validation_board(200),'time_stability':timeblock_stability_board(200),\n            'cost_sensitivity':cost_sensitivity_board(200),'calibration_quality':calibration_quality(),\n            'expected_edge':expected_edge_map(),'signal_readiness':signal_readiness_report(),\n            'v75_parameter_lab':v75_asset_parameter_board(),\n            'portfolio_stress':portfolio_stress()}", 'v75.0 validation integration'),

    # ----- v76.0 Trade Path Intelligence -----
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\ndef trade_path_profile(asset,horizon,direction,setup_name=None,limit=300):\n    """Learn how profitable and losing trades actually travel after entry.\n\n    MFE/MAE are derived only from the path observed after entry.\n    Sparse history remains diagnostic and has no decision influence.\n    """\n    if not pg_enabled() or direction not in (\'LONG\',\'SHORT\'):\n        return {\'status\':\'BUILDING\',\'n\':0,\'decision_influence\':False}\n    try:\n        with pg_connect() as c:\n            rows=c.execute("""SELECT direction,entry_price,high_price,low_price,total_pnl_fraction,payload\n                              FROM shadow_trades\n                              WHERE status<>\'ACTIVE\' AND total_pnl_fraction IS NOT NULL\n                                AND asset=%s AND horizon=%s AND direction=%s\n                              ORDER BY closed_at DESC NULLS LAST LIMIT %s""",\n                           (asset,horizon,direction,int(limit))).fetchall()\n        obs=[]\n        for rr in rows:\n            x=dict(rr)\n            pay=x.get(\'payload\')\n            if not isinstance(pay,dict):\n                try: pay=json.loads(pay or \'{}\')\n                except Exception: pay={}\n            psetup=str(pay.get(\'setup\') or pay.get(\'trade_plan_setup\') or pay.get(\'entry_setup\') or \'\')\n            if setup_name and psetup and psetup!=setup_name:\n                continue\n            entry=float(x.get(\'entry_price\') or 0.0)\n            hi=float(x.get(\'high_price\') or entry)\n            lo=float(x.get(\'low_price\') or entry)\n            pnl=float(x.get(\'total_pnl_fraction\') or 0.0)\n            if entry<=0: continue\n            if direction==\'LONG\':\n                mfe=max(0.0,hi/entry-1.0)\n                mae=max(0.0,1.0-lo/entry)\n            else:\n                mfe=max(0.0,1.0-lo/entry)\n                mae=max(0.0,hi/entry-1.0)\n            capture=(max(0.0,pnl)/mfe) if mfe>1e-9 else None\n            obs.append({\'pnl\':pnl,\'mfe\':mfe,\'mae\':mae,\'capture\':capture})\n        n=len(obs)\n        if not n:\n            return {\'status\':\'BUILDING\',\'n\':0,\'decision_influence\':False}\n        wins=[z for z in obs if z[\'pnl\']>0]\n        losers=[z for z in obs if z[\'pnl\']<=0]\n        def q(vals,p):\n            vals=sorted(float(v) for v in vals if v is not None and math.isfinite(float(v)))\n            if not vals: return None\n            j=(len(vals)-1)*p; a=int(j); b=min(len(vals)-1,a+1); w=j-a\n            return vals[a]*(1-w)+vals[b]*w\n        win_mae=[z[\'mae\'] for z in wins]\n        win_mfe=[z[\'mfe\'] for z in wins]\n        cap=[z[\'capture\'] for z in wins if z[\'capture\'] is not None]\n        influence=n>=20 and len(wins)>=8\n        return {\n            \'status\':\'MEASURABLE\' if influence else \'BUILDING\',\n            \'n\':n,\'wins\':len(wins),\'losses\':len(losers),\n            \'hit_rate\':len(wins)/n,\n            \'median_winner_mfe\':q(win_mfe,0.50),\n            \'p75_winner_mfe\':q(win_mfe,0.75),\n            \'median_winner_mae\':q(win_mae,0.50),\n            \'p80_winner_mae\':q(win_mae,0.80),\n            \'median_capture_ratio\':q(cap,0.50),\n            \'p25_capture_ratio\':q(cap,0.25),\n            \'decision_influence\':influence,\n            \'setup\':setup_name,\n            \'principle\':\'Learn the normal profitable path before tightening stops or taking profit.\'\n        }\n    except Exception as ex:\n        return {\'status\':\'ERROR\',\'n\':0,\'decision_influence\':False,\'error\':f\'{type(ex).__name__}: {ex}\'}\n\n\ndef trade_path_intelligence(asset,horizon,direction,trade_plan):\n    plan=trade_plan or {}\n    setup=str(plan.get(\'setup\') or plan.get(\'reason\') or \'GENERIC\')\n    prof=trade_path_profile(asset,horizon,direction,setup)\n    out={\'status\':prof.get(\'status\'),\'profile\':prof,\'decision_influence\':bool(prof.get(\'decision_influence\'))}\n    if not prof.get(\'decision_influence\'):\n        out.update({\'management_policy\':\'BASELINE\',\'normal_pullback_buffer_pct\':None,\'capture_problem\':False})\n        return out\n\n    normal_pullback=float(prof.get(\'p80_winner_mae\') or 0.0)\n    cap=prof.get(\'median_capture_ratio\')\n    capture_problem=bool(cap is not None and float(cap)<0.35)\n    out.update({\n        \'management_policy\':\'PATH_CONDITIONED\',\n        \'normal_pullback_buffer_pct\':normal_pullback,\n        \'winner_mfe_reference_pct\':prof.get(\'median_winner_mfe\'),\n        \'capture_problem\':capture_problem,\n        \'recommended_behavior\':(\n            \'LET_WINNER_RUN_AND_TRAIL_STRUCTURALLY\'\n            if capture_problem else\n            \'NORMAL_STRUCTURAL_MANAGEMENT\'\n        ),\n        \'rule\':\'Do not tighten a stop inside the p80 adverse excursion historically survived by profitable comparable trades unless thesis invalidates.\'\n    })\n    return out\n\n\ndef trade_path_intelligence_board():\n    items=[]\n    for asset in DISPLAY_ASSETS:\n        for h in HORIZONS:\n            for d in (\'LONG\',\'SHORT\'):\n                z=trade_path_profile(asset,h,d,None)\n                if int(z.get(\'n\') or 0)>0:\n                    items.append({\'asset\':asset,\'horizon\':h,\'direction\':d,**z})\n    measurable=[x for x in items if x.get(\'decision_influence\')]\n    return {\'version\':VERSION,\'status\':\'MEASURABLE\' if measurable else \'BUILDING\',\n            \'items\':items,\'measurable_cells\':len(measurable),\n            \'objective\':\'Increase realized capture of favorable moves while avoiding stops inside normal profitable-trade noise.\'}\n\ndef trade_lifecycle_board(limit=100):', 'v76.0 trade path intelligence engine'),
    ("                trade_plan['institutional_signal']=institutional_signal", "                trade_plan['trade_path_intelligence']=trade_path_intelligence(asset,horizon,research_dec,trade_plan)\n                trade_plan['institutional_signal']=institutional_signal", 'v76.0 path intelligence integration'),
    ("def validation_stack():\n    return {'validation':oos_validation_board(200),'time_stability':timeblock_stability_board(200),\n            'cost_sensitivity':cost_sensitivity_board(200),'calibration_quality':calibration_quality(),\n            'expected_edge':expected_edge_map(),'signal_readiness':signal_readiness_report(),\n            'v75_parameter_lab':v75_asset_parameter_board(),\n            'portfolio_stress':portfolio_stress()}", "def validation_stack():\n    return {'validation':oos_validation_board(200),'time_stability':timeblock_stability_board(200),\n            'cost_sensitivity':cost_sensitivity_board(200),'calibration_quality':calibration_quality(),\n            'expected_edge':expected_edge_map(),'signal_readiness':signal_readiness_report(),\n            'v75_parameter_lab':v75_asset_parameter_board(),\n            'trade_path_intelligence':trade_path_intelligence_board(),\n            'portfolio_stress':portfolio_stress()}", 'v76.0 path intelligence validation board'),
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
            elif label=='v74.0 runtime version' and "veritas-max-product-v75.0-walkforward-profitability-lab" in dst:
                already.append(label+" (superseded by v75)")
            else:
                raise RuntimeError(f"expected pattern not found: {label}")

        if dst != src:
            TARGET.write_text(dst, encoding="utf-8")

        # v75.1: synchronize only the public VERSION label in the auxiliary V70 module.
        try:
            v70p=Path(__file__).resolve().with_name("veritas_v70.py")
            if v70p.exists():
                v70s=v70p.read_text(encoding="utf-8")
                v70n=v70s.replace(
                    'VERSION = "veritas-max-product-v72.0-market-intelligence-causal-decision"',
                    'VERSION = "veritas-max-product-v75.0-walkforward-profitability-lab"',
                    1
                ).replace(
                    "VERSION = 'veritas-max-product-v72.0-market-intelligence-causal-decision'",
                    "VERSION = 'veritas-max-product-v75.0-walkforward-profitability-lab'",
                    1
                )
                if v70n!=v70s:
                    v70p.write_text(v70n,encoding="utf-8")
                    applied.append("v75.1 V70 version sync")
                elif "veritas-max-product-v75.0-walkforward-profitability-lab" in v70s:
                    already.append("v75.1 V70 version sync")
        except Exception as vex:
            print(f"[VERITAS BOOTSTRAP] v75.1 V70 version sync warning: {type(vex).__name__}: {vex}", file=sys.stderr, flush=True)

        print(
            "[VERITAS BOOTSTRAP] v76.0 OK: "
            + "; ".join(applied + [x + " (already)" for x in already]),
            flush=True,
        )
    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v76.0 skipped safely: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

apply()
