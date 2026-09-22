"""
VERITAS v78.0 bootstrap patch.

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
PORTFOLIO_TARGET = Path(__file__).resolve().with_name("veritas_portfolio.py")

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

    # ----- v76.2 Dashboard Fail-Soft -----
    ('setTimeout(()=>ctl.abort(),30000)', 'setTimeout(()=>ctl.abort(),180000)', 'v76.2 dashboard long-request safety timeout'),
    ('setInterval(load,45000)', 'setInterval(load,60000)', 'v76.2 dashboard refresh cadence'),
    ('}catch(e){const sys=document.getElementById(\'sys\');if(sys&&sys.textContent&&sys.textContent.trim()!==\'—\'){sys.innerHTML=\'<span class="warn">UPDATING</span>\'}else if(sys){sys.innerHTML=\'<span class="warn">DEGRADED</span>\'}document.getElementById(\'stamp\').textContent=\'Последний экран сохранён · обновление данных задержано: \'+String(e)}', '}catch(e){const sys=document.getElementById(\'sys\');const aborted=(e&&e.name===\'AbortError\');if(sys&&sys.textContent&&sys.textContent.trim()!==\'—\'){sys.innerHTML=\'<span class="warn">UPDATING</span>\'}else if(sys){sys.innerHTML=\'<span class="warn">DEGRADED</span>\'}document.getElementById(\'stamp\').textContent=aborted?\'Последний экран сохранён · сервер ещё считает новый цикл\':\'Последний экран сохранён · обновление данных задержано: \'+String(e)}', 'v76.2 dashboard abort fail-soft message'),

    # ----- v77.0 Decision Quality Stack -----
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\n# ---------------- v77 Decision Quality Stack ----------------\n\n_V77_RECENT_TRADE_CACHE = {}\n_V77_RECENT_TRADE_CACHE_SECONDS = 60\n\ndef _v77_new_setup_signature(asset,horizon,direction,setup,trigger):\n    try:\n        t=\'na\' if trigger is None else f\'{float(trigger):.8g}\'\n    except Exception:\n        t=str(trigger or \'na\')\n    return f\'{asset}:{horizon}:{direction}:{setup}:{t}\'\n\ndef _v77_recent_closed_trade(asset,horizon,direction):\n    """Light cache used only for churn suppression/re-entry logic."""\n    key=(asset,horizon,direction)\n    cached=_V77_RECENT_TRADE_CACHE.get(key)\n    if cached and time.time()-cached[0]<_V77_RECENT_TRADE_CACHE_SECONDS:\n        return cached[1]\n    if not pg_enabled():\n        return None\n    try:\n        with pg_connect() as c:\n            r=c.execute("""SELECT status,closed_at,entry_price,exit_price,payload\n                           FROM shadow_trades\n                           WHERE status<>\'ACTIVE\' AND asset=%s AND horizon=%s AND direction=%s\n                           ORDER BY closed_at DESC NULLS LAST LIMIT 1""",\n                        (asset,horizon,direction)).fetchone()\n        out=dict(r) if r else None\n        _V77_RECENT_TRADE_CACHE[key]=(time.time(),out)\n        return out\n    except Exception:\n        return None\n\ndef _v77_valid_structural_stop(entry,direction,candidates,pre_impulse=None):\n    """Choose a structural invalidation first. Never tighten stop merely to improve RR."""\n    entry=float(entry or 0.0)\n    if entry<=0:\n        return None,None\n    usable=[]\n    for x in candidates or []:\n        try:\n            sp=float(x.get(\'stop_price\'))\n            method=str(x.get(\'method\') or \'\')\n        except Exception:\n            continue\n        if direction==\'LONG\' and sp>=entry: continue\n        if direction==\'SHORT\' and sp<=entry: continue\n        if method in (\'RECENT_SWING_LOW_HIGH\',\'STRUCTURAL_INVALIDATION\',\'CONFIRMED_PULLBACK_LOW_HIGH\',\'SESSION_EXTREME\'):\n            usable.append((method,sp))\n    # A stored pre-impulse anchor is preferred when the plan already produced a valid stop from it.\n    # Otherwise use the nearest genuinely structural stop, not BREAKOUT_LEVEL.\n    priority=(\'RECENT_SWING_LOW_HIGH\',\'CONFIRMED_PULLBACK_LOW_HIGH\',\'STRUCTURAL_INVALIDATION\',\'SESSION_EXTREME\')\n    for m in priority:\n        rows=[z for z in usable if z[0]==m]\n        if rows:\n            if direction==\'LONG\':\n                return max(rows,key=lambda z:z[1])[1],m\n            return min(rows,key=lambda z:z[1])[1],m\n    return None,None\n\ndef _v77_regime_shift_state(f, candidate_direction):\n    st=f.get(\'intraday_structure\') or {}\n    hs=f.get(\'horizon_structure\') or {}\n    ti=f.get(\'trend_impulse\') or {}\n    gen=f.get(\'impulse_genesis\') or {}\n    piv=f.get(\'impulse_pivot_break\') or {}\n    transition=(f.get(\'institutional_signal\') or {}).get(\'regime_transition\') or {}\n    old_failed=str(st.get(\'lifecycle\') or \'\')==\'FAILURE\' or str(ti.get(\'entry_quality\') or \'\')==\'INVALIDATED\'\n    fast_break=bool(\n        (gen.get(\'candidate_direction\')==candidate_direction and\n         (gen.get(\'active\') or float(gen.get(\'probability\') or 0)>=0.68))\n        or\n        (piv.get(\'candidate_direction\')==candidate_direction and\n         (piv.get(\'active\') or float(piv.get(\'probability\') or 0)>=0.72))\n    )\n    path=max(float(gen.get(\'local_efficiency\') or 0.0),float(piv.get(\'local_efficiency\') or 0.0))\n    confirmations=max(int(gen.get(\'confirmations\') or 0),int(piv.get(\'confirmations\') or 0))\n    native_dir=str(hs.get(\'direction\') or \'NO_TRADE\')\n    native_not_opposed=native_dir in (\'NO_TRADE\',candidate_direction)\n    trans_state=str(transition.get(\'state\') or \'\')\n    if fast_break and old_failed and path>=0.35 and confirmations>=3 and native_not_opposed:\n        return \'NEW_REGIME_PROVISIONAL\'\n    if fast_break and confirmations>=4 and path>=0.45 and native_dir==candidate_direction:\n        return \'NEW_REGIME_ACCEPTED\'\n    if old_failed:\n        return \'OLD_REGIME_WEAKENING\'\n    if trans_state in (\'TRANSITION\',\'DESTABILIZING\'):\n        return \'TRANSITION\'\n    return \'STABLE\'\n\ndef _v77_error_cost_profile(plan):\n    """Cost attribution is attached to the decision so later learning penalizes the right engine."""\n    return {\n        \'direction_error_weight\':1.00,\n        \'stop_execution_error_weight\':0.85,\n        \'late_entry_error_weight\':0.70,\n        \'premature_exit_error_weight\':0.80,\n        \'missed_impulse_opportunity_weight\':0.90,\n        \'small_early_probe_false_break_weight\':0.30,\n        \'churn_error_weight\':0.65,\n        \'principle\':\'Do not punish direction model for a correct-direction trade lost by stop, timing or exit.\'\n    }\n\ndef v77_decision_quality_stack(asset,horizon,f,trade_plan,research_dec):\n    """Post-process an otherwise formed trade plan.\n\n    Priorities:\n    1) a genuinely new setup is not vetoed by the previous setup\'s FAILURE state;\n    2) stop location is structural, position size absorbs the distance;\n    3) transition entries are staged, never full-size immediately;\n    4) re-entry needs a new market event, reducing ENTRY/EXIT churn;\n    5) late continuation remains allowed when remaining economics are positive.\n    """\n    plan=dict(trade_plan or {})\n    direction=str(research_dec or plan.get(\'direction\') or \'NO_TRADE\')\n    if direction not in (\'LONG\',\'SHORT\'):\n        plan[\'v77\']={\'status\':\'NO_DIRECTION\',\'error_cost\':_v77_error_cost_profile(plan)}\n        return plan\n\n    gen=f.get(\'impulse_genesis\') or {}\n    piv=f.get(\'impulse_pivot_break\') or {}\n    rev=f.get(\'tactical_reversal\') or {}\n    rng=f.get(\'range_retest_breakout\') or {}\n    st=f.get(\'intraday_structure\') or {}\n    hs=f.get(\'horizon_structure\') or {}\n    inst=f.get(\'institutional_signal\') or {}\n\n    candidate=None\n    for x in (gen,piv,rev,rng):\n        cd=str(x.get(\'direction\') or x.get(\'candidate_direction\') or \'\')\n        if cd==direction:\n            if candidate is None or float(x.get(\'probability\') or 0)>float(candidate.get(\'probability\') or 0):\n                candidate=x\n    candidate=candidate or {}\n\n    setup=str(plan.get(\'setup\') or candidate.get(\'setup\') or plan.get(\'reason\') or \'GENERIC\')\n    trigger=plan.get(\'trigger_level\')\n    if trigger is None:\n        trigger=candidate.get(\'trigger_level\')\n    if trigger is None:\n        if direction==\'LONG\':\n            trigger=(f.get(\'structural_levels\') or {}).get(\'resistance\')\n        else:\n            trigger=(f.get(\'structural_levels\') or {}).get(\'support\')\n    setup_id=_v77_new_setup_signature(asset,horizon,direction,setup,trigger)\n    plan[\'setup_id\']=setup_id\n\n    shift=_v77_regime_shift_state(f,direction)\n    plan[\'regime_shift_state\']=shift\n\n    # --- State Conflict Resolver ---\n    old_entry_invalid=str(st.get(\'entry_quality\') or \'\')==\'INVALIDATED\' or str(st.get(\'lifecycle\') or \'\')==\'FAILURE\'\n    prob=float(candidate.get(\'probability\') or 0.0)\n    conf=int(candidate.get(\'confirmations\') or 0)\n    fast_evidence=bool(\n        candidate and\n        (candidate.get(\'active\') or prob>=0.72) and\n        conf>=3 and\n        shift in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\')\n    )\n    thesis_status=str(f.get(\'v70_thesis_status\') or plan.get(\'v70_thesis_status\') or \'\')\n    hard_thesis_fail=thesis_status in (\'BROKEN\',\'INVALIDATED\') or str(f.get(\'v70_gate_class\') or \'\')==\'THESIS_VETO\'\n\n    if old_entry_invalid and fast_evidence and not hard_thesis_fail:\n        # New setup has its own identity; previous failed setup cannot veto it.\n        plan[\'eligible\']=True\n        plan[\'reason\']=\'v77_new_setup_resets_old_entry_failure\'\n        plan[\'entry_quality\']=\'NEW_SETUP_PROVISIONAL\'\n        plan[\'state_conflict_override\']=True\n\n    # --- Structural Stop Enforcement ---\n    impulse_like=setup in (\'IMPULSE_GENESIS\',\'IMPULSE_PIVOT_BREAK\',\'TACTICAL_REVERSAL\',\'BRENT_REVERSAL_CAPTURE\') \\\n        or shift in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\')\n    entry=float(plan.get(\'entry_price\') or f.get(\'price\') or 0.0)\n    if impulse_like and entry>0:\n        structural_stop,method=_v77_valid_structural_stop(entry,direction,plan.get(\'stop_candidates\') or [],\n                                                          plan.get(\'pre_impulse_swing\'))\n        # Candidate-generated pre-impulse stop may be the best available structural anchor.\n        if candidate.get(\'structural_stop_policy\')==\'PRE_IMPULSE_SWING\' and candidate.get(\'stop_price\') is not None:\n            try:\n                csp=float(candidate.get(\'stop_price\'))\n                if (direction==\'LONG\' and csp<entry) or (direction==\'SHORT\' and csp>entry):\n                    structural_stop=csp; method=\'PRE_IMPULSE_SWING\'\n            except Exception:\n                pass\n        if structural_stop is not None:\n            plan[\'stop_price\']=structural_stop\n            plan[\'stop_method\']=method\n            sd=abs(entry-structural_stop)/entry\n            plan[\'stop_distance_pct\']=sd\n            exp=float(plan.get(\'expected_move_pct\') or 0.0)\n            plan[\'expected_to_stop_ratio\']=exp/sd if sd>1e-12 else 999.0\n            plan[\'structural_stop_enforced\']=True\n\n    # --- Remaining Move / continuation economics ---\n    exp=float(plan.get(\'expected_move_pct\') or 0.0)\n    sd=float(plan.get(\'stop_distance_pct\') or 0.0)\n    rr=exp/sd if sd>1e-12 else float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n    late=bool(plan.get(\'late_entry\') or (st.get(\'late_entry\')))\n    plan[\'entry_lateness_policy\']=\'ALLOWED_IF_REMAINING_EDGE_POSITIVE\'\n    if late and plan.get(\'eligible\'):\n        if exp<=0 or rr<0.75:\n            plan[\'eligible\']=False\n            plan[\'reason\']=\'late_entry_insufficient_remaining_edge\'\n        else:\n            plan[\'continuation_entry\']=True\n\n    # --- Staged Position Sizing ---\n    initial=float(plan.get(\'initial_position_fraction\') or 0.0)\n    transition_state=shift in (\'TRANSITION\',\'OLD_REGIME_WEAKENING\',\'NEW_REGIME_PROVISIONAL\')\n    accepted=shift==\'NEW_REGIME_ACCEPTED\'\n    volume_ok=bool((st.get(\'volume_confirmed\')) or float(candidate.get(\'local_volume_ratio\') or 0)>=1.0)\n    if plan.get(\'eligible\'):\n        if transition_state:\n            initial=min(initial if initial>0 else 0.10,0.10)\n            stage=\'PROBE_5_10\'\n        elif accepted and volume_ok:\n            initial=min(initial if initial>0 else 0.25,0.25)\n            stage=\'ACCEPTED_15_25\'\n        elif accepted:\n            initial=min(initial if initial>0 else 0.15,0.15)\n            stage=\'ACCEPTED_LOW_VOLUME_10_15\'\n        else:\n            stage=\'BASELINE\'\n        plan[\'initial_position_fraction\']=initial\n        plan[\'position_stage\']=stage\n        plan[\'scaling_policy\']=\'V77_PROBE_THEN_ACCEPTANCE_THEN_CONFIRM\'\n\n    # If structural economics are poor, reduce size / reject; never tighten the stop to fake RR.\n    rr=float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n    if plan.get(\'eligible\') and impulse_like:\n        if rr<0.60:\n            plan[\'eligible\']=False\n            plan[\'reason\']=\'structural_stop_economics_insufficient\'\n            plan[\'initial_position_fraction\']=0.0\n        elif rr<0.90:\n            plan[\'initial_position_fraction\']=min(float(plan.get(\'initial_position_fraction\') or 0.0),0.05)\n            plan[\'position_stage\']=\'MICRO_PROBE_POOR_RR\'\n\n    # --- Churn suppression / Re-entry intelligence ---\n    recent=_v77_recent_closed_trade(asset,horizon,direction)\n    reentry={\'checked\':bool(recent),\'allowed\':True,\'new_event_required\':False}\n    if recent:\n        payload=recent.get(\'payload\')\n        if not isinstance(payload,dict):\n            try: payload=json.loads(payload or \'{}\')\n            except Exception: payload={}\n        old_setup=str(payload.get(\'setup_id\') or \'\')\n        old_trigger=payload.get(\'trigger_level\')\n        closed_at=recent.get(\'closed_at\')\n        age_min=None\n        try:\n            if isinstance(closed_at,str):\n                dt=datetime.fromisoformat(closed_at.replace(\'Z\',\'+00:00\'))\n            else:\n                dt=closed_at\n            if dt and dt.tzinfo is None: dt=dt.replace(tzinfo=timezone.utc)\n            if dt: age_min=max(0.0,(datetime.now(timezone.utc)-dt).total_seconds()/60.0)\n        except Exception:\n            pass\n\n        new_event=bool(\n            gen.get(\'active\') or piv.get(\'active\') or\n            str(rng.get(\'state\') or \'\') in (\'RETEST_ENTRY\',\'BREAKOUT_ADD\') or\n            shift==\'NEW_REGIME_ACCEPTED\'\n        )\n        trigger_changed=False\n        try:\n            if old_trigger is not None and trigger is not None and float(trigger)!=0:\n                trigger_changed=abs(float(trigger)-float(old_trigger))/abs(float(trigger))>=0.0015\n        except Exception:\n            pass\n\n        same_setup=bool(old_setup and old_setup==setup_id)\n        if age_min is not None and age_min<20 and same_setup and not (new_event or trigger_changed):\n            reentry.update({\'allowed\':False,\'new_event_required\':True,\'age_minutes\':round(age_min,2),\n                            \'reason\':\'same_setup_recently_closed_without_new_structure\'})\n            if plan.get(\'eligible\'):\n                plan[\'eligible\']=False\n                plan[\'reason\']=\'v77_churn_suppression_wait_new_event\'\n                plan[\'initial_position_fraction\']=0.0\n        elif age_min is not None and age_min<45 and (new_event or trigger_changed):\n            reentry.update({\'allowed\':True,\'new_event_required\':False,\'age_minutes\':round(age_min,2),\n                            \'reason\':\'new_reclaim_pivot_or_retest_event\'})\n            plan[\'reentry_candidate\']=True\n\n    plan[\'reentry_intelligence\']=reentry\n\n    # --- Exit Alpha policy metadata ---\n    path=plan.get(\'trade_path_intelligence\') or {}\n    path_prof=path.get(\'profile\') or {}\n    plan[\'exit_alpha\']={\n        \'mode\':\'STRUCTURE_FIRST\',\n        \'close_on_entry_invalidation_only\':False,\n        \'full_exit_requires\':\'SETUP_OR_THESIS_INVALIDATION\',\n        \'reduce_on_timing_deterioration\':True,\n        \'normal_pullback_buffer_pct\':path.get(\'normal_pullback_buffer_pct\'),\n        \'capture_problem\':bool(path.get(\'capture_problem\')),\n        \'recommendation\':path.get(\'recommended_behavior\') or \'HOLD_UNTIL_STRUCTURE_CHANGES\',\n        \'principle\':\'Do not convert a temporary timing failure into a full thesis exit.\'\n    }\n\n    # --- Trade Quality Score ---\n    dir_q=min(1.0,max(0.0,float(candidate.get(\'probability\') or abs(float(f.get(\'score\') or 0.0)))))\n    timing_q=1.0 if not late else 0.65\n    stop_q=1.0 if plan.get(\'structural_stop_enforced\') else 0.55\n    remaining_q=min(1.0,max(0.0,rr/1.5)) if rr else 0.0\n    regime_q=1.0 if accepted else 0.75 if transition_state else 0.65\n    tq=(dir_q*timing_q*stop_q*remaining_q*regime_q)**0.2 if all(x>0 for x in (dir_q,timing_q,stop_q,remaining_q,regime_q)) else 0.0\n    plan[\'trade_quality_score\']=round(tq,4)\n    plan[\'trade_quality_components\']={\n        \'direction\':round(dir_q,4),\'timing\':timing_q,\'stop\':stop_q,\n        \'remaining_move\':round(remaining_q,4),\'regime_fit\':regime_q\n    }\n    plan[\'error_cost\']=_v77_error_cost_profile(plan)\n    plan[\'v77\']={\n        \'status\':\'ACTIVE\',\'setup_id\':setup_id,\'regime_shift_state\':shift,\n        \'old_failure_reset\':bool(plan.get(\'state_conflict_override\')),\n        \'structural_stop_enforced\':bool(plan.get(\'structural_stop_enforced\')),\n        \'position_stage\':plan.get(\'position_stage\'),\n        \'reentry\':reentry\n    }\n    return plan\n\ndef trade_lifecycle_board(limit=100):', 'v77.0 decision quality engine'),
    ("                trade_plan['trade_path_intelligence']=trade_path_intelligence(asset,horizon,research_dec,trade_plan)\n                trade_plan['institutional_signal']=institutional_signal", "                trade_plan['trade_path_intelligence']=trade_path_intelligence(asset,horizon,research_dec,trade_plan)\n                trade_plan=v77_decision_quality_stack(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", 'v77.0 decision quality integration'),
    ("VERSION = 'veritas-max-product-v75.0-walkforward-profitability-lab'", "VERSION = 'veritas-max-product-v77.0-decision-quality-stack'", 'v77.0 runtime version'),

    # ----- v78.0 Impulse Portfolio + Execution Consistency -----
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\ndef execution_consistency_layer(asset,horizon,f,trade_plan,research_dec):\n    """Global final consistency gate before any portfolio/execution layer.\n\n    It does not invent a signal. It ensures that direction, setup identity,\n    stop, position stage, re-entry state and exit semantics are mutually\n    consistent. Corrections reduce risk; the layer never increases risk.\n    """\n    plan=dict(trade_plan or {})\n    direction=str(research_dec or plan.get(\'direction\') or \'NO_TRADE\')\n    corrections=[]\n    veto=None\n\n    if direction not in (\'LONG\',\'SHORT\'):\n        plan[\'execution_consistency\']={\n            \'status\':\'NO_DIRECTION\',\'corrections\':[],\'veto\':None,\n            \'principle\':\'No directional execution without a directional research decision.\'\n        }\n        return plan\n\n    entry=float(plan.get(\'entry_price\') or f.get(\'price\') or 0.0)\n    stop=plan.get(\'stop_price\')\n    v77=plan.get(\'v77\') or {}\n    shift=str(plan.get(\'regime_shift_state\') or v77.get(\'regime_shift_state\') or \'STABLE\')\n    reentry=plan.get(\'reentry_intelligence\') or {}\n    exit_alpha=plan.get(\'exit_alpha\') or {}\n    hard_thesis_fail=str(f.get(\'v70_thesis_status\') or \'\') in (\'BROKEN\',\'INVALIDATED\') or str(f.get(\'v70_gate_class\') or \'\')==\'THESIS_VETO\'\n\n    # Direction must be internally consistent.\n    pd=str(plan.get(\'direction\') or direction)\n    if pd in (\'LONG\',\'SHORT\') and pd!=direction:\n        veto=\'DIRECTION_CONFLICT\'\n\n    # Hard thesis failure always dominates entry logic.\n    if hard_thesis_fail:\n        veto=\'THESIS_INVALIDATED\'\n\n    # Re-entry suppression is a hard admission gate.\n    if reentry and reentry.get(\'allowed\') is False:\n        veto=\'REENTRY_BLOCKED_NO_NEW_EVENT\'\n\n    # An eligible plan must have a valid stop on the correct side.\n    if plan.get(\'eligible\'):\n        if stop is None or entry<=0:\n            veto=veto or \'MISSING_STOP_OR_ENTRY\'\n        else:\n            try:\n                s=float(stop)\n                wrong=(direction==\'LONG\' and s>=entry) or (direction==\'SHORT\' and s<=entry)\n                if wrong:\n                    veto=veto or \'STOP_WRONG_SIDE\'\n            except Exception:\n                veto=veto or \'INVALID_STOP\'\n\n    # Impulse/reversal setups require a structural stop except explicit micro-probes.\n    setup=str(plan.get(\'setup\') or \'\')\n    impulse_like=bool(\n        setup in (\'IMPULSE_GENESIS\',\'IMPULSE_PIVOT_BREAK\',\'TACTICAL_REVERSAL\',\'BRENT_REVERSAL_CAPTURE\')\n        or shift in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\')\n    )\n    stage=str(plan.get(\'position_stage\') or \'\')\n    if plan.get(\'eligible\') and impulse_like and not plan.get(\'structural_stop_enforced\'):\n        if stage!=\'MICRO_PROBE_POOR_RR\':\n            veto=veto or \'IMPULSE_WITHOUT_STRUCTURAL_STOP\'\n\n    # Stage and size must agree. Never increase risk here.\n    size=float(plan.get(\'initial_position_fraction\') or 0.0)\n    caps={\n        \'PROBE_5_10\':0.10,\n        \'ACCEPTED_LOW_VOLUME_10_15\':0.15,\n        \'ACCEPTED_15_25\':0.25,\n        \'MICRO_PROBE_POOR_RR\':0.05,\n    }\n    cap=caps.get(stage)\n    if cap is not None and size>cap:\n        plan[\'initial_position_fraction\']=cap\n        corrections.append(f\'size_capped_{stage}_{cap:.2f}\')\n\n    # Late entries remain allowed only with remaining economics.\n    if bool(plan.get(\'late_entry\') or (f.get(\'intraday_structure\') or {}).get(\'late_entry\')):\n        rr=float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n        if rr<0.75:\n            veto=veto or \'LATE_ENTRY_INSUFFICIENT_REMAINING_EDGE\'\n\n    # ENTRY invalidation is timing information, not an automatic full-exit instruction.\n    if exit_alpha:\n        exit_alpha[\'close_on_entry_invalidation_only\']=False\n        exit_alpha[\'full_exit_requires\']=\'SETUP_OR_THESIS_INVALIDATION\'\n        plan[\'exit_alpha\']=exit_alpha\n\n    # Quality score controls size, not signal direction.\n    tq=float(plan.get(\'trade_quality_score\') or 0.0)\n    if plan.get(\'eligible\') and tq>0 and tq<0.45:\n        old=float(plan.get(\'initial_position_fraction\') or 0.0)\n        new=min(old,0.05)\n        if new<old:\n            plan[\'initial_position_fraction\']=new\n            corrections.append(\'low_trade_quality_micro_probe\')\n\n    if veto:\n        plan[\'eligible\']=False\n        plan[\'initial_position_fraction\']=0.0\n        plan[\'reason\']=\'execution_consistency_veto:\'+veto\n\n    plan[\'execution_consistency\']={\n        \'status\':\'VETO\' if veto else (\'CORRECTED\' if corrections else \'PASS\'),\n        \'veto\':veto,\'corrections\':corrections,\n        \'direction\':direction,\'setup_id\':plan.get(\'setup_id\'),\n        \'structural_stop\':bool(plan.get(\'structural_stop_enforced\')),\n        \'position_stage\':plan.get(\'position_stage\'),\n        \'principle\':\'Signal, setup, structural stop, size and exit semantics must describe the same trade.\'\n    }\n    return plan\n\ndef trade_lifecycle_board(limit=100):', 'v78.0 execution consistency layer'),
    ("                trade_plan=v77_decision_quality_stack(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", "                trade_plan=v77_decision_quality_stack(asset,horizon,f,trade_plan,research_dec)\n                trade_plan=execution_consistency_layer(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", 'v78.0 execution consistency integration'),
    ("VERSION = 'veritas-max-product-v77.0-decision-quality-stack'", "VERSION = 'veritas-max-product-v78.0-impulse-portfolio-consistency'", 'v78.0 runtime version'),
    # ----- v79.0 Trade Integrity / Win-Rate Layer -----
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\n# ---------------- v79.0 Trade Integrity / Win-Rate Layer ----------------\n\ndef trade_integrity_layer(asset,horizon,f,trade_plan,research_dec):\n    """Final trade-state separation for win-rate quality.\n\n    Directional thesis and permission to enter are different states.\n    A temporary timing deterioration does not erase a valid thesis, but it\n    can block a fresh entry until the fast conflict resolves.\n    """\n    plan=dict(trade_plan or {})\n    direction=str(research_dec or plan.get(\'direction\') or \'NO_TRADE\')\n    thesis=str(f.get(\'v70_thesis_status\') or plan.get(\'v70_thesis_status\') or \'\')\n    gate=str(f.get(\'v70_gate_class\') or \'\')\n    st=f.get(\'intraday_structure\') or {}\n    piv=f.get(\'impulse_pivot_break\') or {}\n    rev=f.get(\'tactical_reversal\') or {}\n    ec=plan.get(\'execution_consistency\') or {}\n    arb=plan.get(\'rule_arbitration\') or {}\n\n    hard=False\n    hard_reasons=[]\n    if gate==\'THESIS_VETO\' or thesis in (\'BROKEN\',\'INVALIDATED\'):\n        hard=True; hard_reasons.append(\'THESIS_INVALIDATION\')\n    if ec.get(\'status\')==\'VETO\':\n        hard=True; hard_reasons.append(\'EXECUTION_CONSISTENCY_VETO\')\n    if (plan.get(\'reentry_intelligence\') or {}).get(\'allowed\') is False:\n        hard=True; hard_reasons.append(\'REENTRY_BLOCKED\')\n    if (arb.get(\'hard_veto\') or {}).get(\'decision\')==\'VETO\':\n        hard=True; hard_reasons.append(\'RULE_ARBITRATION_VETO\')\n\n    def opp_fast(x):\n        cd=str(x.get(\'direction\') or x.get(\'candidate_direction\') or \'NO_TRADE\')\n        pr=float(x.get(\'probability\') or 0.0)\n        cf=int(x.get(\'confirmations\') or 0)\n        active=bool(x.get(\'active\'))\n        return direction in (\'LONG\',\'SHORT\') and cd in (\'LONG\',\'SHORT\') and cd!=direction and (active or (pr>=0.72 and cf>=3))\n\n    fast_conflict=opp_fast(piv) or opp_fast(rev)\n\n    # Soft timing conflict: valid thesis but current entry timing is poor.\n    soft_reasons=[]\n    entryq=str(plan.get(\'entry_quality\') or st.get(\'entry_quality\') or \'\')\n    lifecycle=str(st.get(\'lifecycle\') or \'\')\n    if entryq in (\'INVALIDATED\',\'WAIT_CONFIRMATION\',\'LOWER_TF_CAUTION\'):\n        soft_reasons.append(\'TIMING_NOT_READY\')\n    if lifecycle==\'FAILURE\' and not bool((plan.get(\'v77\') or {}).get(\'old_failure_reset\')):\n        soft_reasons.append(\'OLD_OR_CURRENT_SETUP_FAILURE\')\n    if fast_conflict:\n        soft_reasons.append(\'OPPOSITE_FAST_IMPULSE\')\n    if direction in (\'LONG\',\'SHORT\') and not bool(plan.get(\'eligible\')) and not hard:\n        soft_reasons.append(\'PLAN_NOT_ELIGIBLE\')\n\n    if direction not in (\'LONG\',\'SHORT\'):\n        permission=\'NO_DIRECTION\'\n    elif hard:\n        permission=\'VETO\'\n    elif soft_reasons:\n        permission=\'WAIT_ENTRY\'\n    else:\n        permission=\'ENTER\'\n\n    plan[\'trade_integrity\']={\n        \'status\':\'HARD_INVALIDATION\' if hard else (\'SOFT_CONFLICT\' if soft_reasons else \'PASS\'),\n        \'direction_state\':direction,\n        \'thesis_state\':thesis or \'UNSPECIFIED\',\n        \'entry_permission\':permission,\n        \'hard_invalidation\':hard,\n        \'hard_reasons\':hard_reasons,\n        \'soft_reasons\':soft_reasons,\n        \'fast_tf_conflict\':fast_conflict,\n        \'setup_id\':plan.get(\'setup_id\'),\n        \'execution_horizon\':horizon,\n        \'principle\':\'Direction != entry permission. Soft timing deterioration requires confirmation before exit; hard structural/thesis invalidation exits immediately.\'\n    }\n    return plan\n\ndef trade_lifecycle_board(limit=100):', 'v79.0 trade integrity engine'),
    ("                trade_plan=system_rule_arbitration(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", "                trade_plan=system_rule_arbitration(asset,horizon,f,trade_plan,research_dec)\n                trade_plan=trade_integrity_layer(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", 'v79.0 trade integrity integration'),
    ("VERSION = 'veritas-max-product-v78.0-impulse-portfolio-consistency'", "VERSION = 'veritas-max-product-v78.1-rule-experience-arbitration'", 'v78.1 runtime version'),
    ("VERSION = 'veritas-max-product-v78.1-rule-experience-arbitration'", "VERSION = 'veritas-max-product-v79.0-trade-integrity-winrate'", 'v79.0 runtime version'),

    # ----- v79.1 runtime -----
    ("VERSION = 'veritas-max-product-v79.0-trade-integrity-winrate'", "VERSION = 'veritas-max-product-v79.1-signal-first-portfolio-repair'", 'v79.1 runtime version'),

    # ----- v80.0 Unified Execution Core -----
    ('\n\ndef _signed_trade_return(direction,entry,price):', '\n\n\n# ---------------- v80.0 Unified Execution Core ----------------\n\ndef _uec_setup_family(x):\n    plan=x.get(\'trade_plan\') or {}\n    piv=x.get(\'impulse_pivot_break\') or {}\n    rev=x.get(\'tactical_reversal\') or {}\n    rng=x.get(\'range_retest_breakout\') or {}\n    bq=(x.get(\'institutional_signal\') or {}).get(\'breakout_quality\') or {}\n    if piv.get(\'active\'): return \'IMPULSE_PIVOT_BREAK\'\n    if rev.get(\'active\'): return \'TACTICAL_REVERSAL\'\n    if rng.get(\'active\'): return \'RANGE_RETEST_BREAKOUT\'\n    if str(bq.get(\'state\') or \'\') in (\'EARLY_BREAKOUT\',\'CONFIRMED_BREAKOUT\'): return \'BREAKOUT\'\n    if str(plan.get(\'regime_shift_state\') or \'\') in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\'): return \'REGIME_SHIFT\'\n    return \'TREND\'\n\ndef _uec_anchor(x, direction):\n    plan=x.get(\'trade_plan\') or {}\n    levels=x.get(\'structural_levels\') or {}\n    vals=[\n        plan.get(\'breakout_level\'),\n        plan.get(\'recent_swing_anchor\'),\n        levels.get(\'resistance\') if direction==\'LONG\' else levels.get(\'support\'),\n        plan.get(\'invalidation_price\'),\n        x.get(\'price\'),\n    ]\n    for v in vals:\n        try:\n            if v is not None and float(v)>0:\n                return float(v)\n        except Exception:\n            pass\n    return 0.0\n\ndef _uec_anchor_key(v):\n    try:\n        v=float(v)\n        if v<=0: return \'na\'\n        # Four significant digits makes the setup stable through minor noise\n        # while allowing a materially new pivot to create a new setup.\n        return f\'{v:.4g}\'\n    except Exception:\n        return \'na\'\n\ndef _uec_canonical_setup_id(asset,direction,x):\n    family=_uec_setup_family(x)\n    anchor=_uec_anchor(x,direction)\n    raw=f\'{asset}|{direction}|{family}|{_uec_anchor_key(anchor)}\'\n    return \'UTS_\'+hashlib.sha256(raw.encode()).hexdigest()[:20]\n\ndef _uec_row_rank(x):\n    d=str(x.get(\'research_decision\') or \'NO_TRADE\')\n    if d not in (\'LONG\',\'SHORT\'): return -999.0\n    plan=x.get(\'trade_plan\') or {}\n    ti=plan.get(\'trade_integrity\') or {}\n    ec=plan.get(\'execution_consistency\') or {}\n    if ti.get(\'hard_invalidation\') or ec.get(\'status\')==\'VETO\':\n        return -500.0\n    inst=x.get(\'institutional_signal\') or {}\n    ev=inst.get(\'evidence_independence\') or {}\n    bq=inst.get(\'breakout_quality\') or {}\n    hs=x.get(\'horizon_structure\') or {}\n    conf=float(x.get(\'confidence\') or 0.0)\n    indep=int(ev.get(\'independent_count\') or 0)\n    q=float(bq.get(\'quality_score\') or 0.0)\n    hscore=float(hs.get(\'score\') or 0.0)\n    rr=float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n    h={\'1h\':0.00,\'4h\':0.02,\'1d\':0.04,\'3d\':0.025,\'7d\':0.015}.get(str(x.get(\'horizon\') or \'\'),0.0)\n    return conf+0.06*min(indep,5)+0.12*q+0.10*hscore+0.04*min(max(rr,0.0),2.0)+h\n\ndef _uec_asset_candidates(summary):\n    """One canonical directional trade candidate per asset.\n\n    Multiple horizons are evidence for one trade, not separate trades.\n    """\n    by_asset={}\n    for r0 in summary or []:\n        r=dict(r0)\n        d=str(r.get(\'research_decision\') or \'NO_TRADE\')\n        tr=r.get(\'tactical_reversal\') or {}\n        if tr.get(\'active\') and tr.get(\'direction\') in (\'LONG\',\'SHORT\'):\n            d=str(tr.get(\'direction\')); r[\'research_decision\']=d\n        if d not in (\'LONG\',\'SHORT\'): continue\n        if not bool(r.get(\'source_gate_pass\',True)): continue\n        if not bool(r.get(\'market_open\',True)): continue\n        by_asset.setdefault(str(r.get(\'asset\')),[]).append(r)\n\n    out={}\n    for asset,rows in by_asset.items():\n        # Sum support by direction, then choose the best execution row inside\n        # the dominant direction. This prevents one noisy horizon from flipping\n        # a stable multi-timeframe trade.\n        support={\'LONG\':0.0,\'SHORT\':0.0}\n        supporting={\'LONG\':[],\'SHORT\':[]}\n        for r in rows:\n            d=str(r.get(\'research_decision\'))\n            rk=max(-1.0,_uec_row_rank(r))\n            support[d]+=max(0.01,rk)\n            supporting[d].append(str(r.get(\'horizon\')))\n        if not support[\'LONG\'] and not support[\'SHORT\']: continue\n        direction=\'LONG\' if support[\'LONG\']>=support[\'SHORT\'] else \'SHORT\'\n        eligible=[r for r in rows if str(r.get(\'research_decision\'))==direction]\n        if not eligible: continue\n        best=max(eligible,key=_uec_row_rank)\n        best=dict(best)\n        best[\'_uec_direction_support\']=support\n        best[\'_uec_supporting_horizons\']=sorted(set(supporting[direction]),key=lambda h:[\'1h\',\'4h\',\'1d\',\'3d\',\'7d\'].index(h) if h in [\'1h\',\'4h\',\'1d\',\'3d\',\'7d\'] else 99)\n        best[\'_uec_rank\']=_uec_row_rank(best)\n        best[\'_uec_setup_id\']=_uec_canonical_setup_id(asset,direction,best)\n        out[asset]=best\n    return out\n\ndef _uec_hard_terminal(x, current_direction):\n    plan=(x or {}).get(\'trade_plan\') or {}\n    ti=plan.get(\'trade_integrity\') or {}\n    ec=plan.get(\'execution_consistency\') or {}\n    arb=plan.get(\'rule_arbitration\') or {}\n    if ti.get(\'hard_invalidation\'):\n        return \'INVALIDATION\',\'hard_trade_integrity\'\n    if ec.get(\'status\')==\'VETO\':\n        return \'INVALIDATION\',\'execution_consistency_veto\'\n    hv=arb.get(\'hard_veto\') or {}\n    if hv.get(\'decision\')==\'VETO\':\n        return \'INVALIDATION\',\'rule_arbitration_veto\'\n    d=str((x or {}).get(\'research_decision\') or \'NO_TRADE\')\n    if d in (\'LONG\',\'SHORT\') and d!=current_direction:\n        return \'EXIT\',\'confirmed_dominant_direction_flip\'\n    return None,None\n\ndef manage_trade_alerts(summary):\n    """Unified asset-level setup manager.\n\n    One asset/direction/structural setup = one trade state.\n    Horizons confirm the trade; they no longer create parallel trades.\n    Soft timing failures never terminate a trade.\n    """\n    if not TRADE_ALERTS_ENABLED or not pg_enabled(): return []\n    candidates=_uec_asset_candidates(summary)\n    out=[]; alert_rows=[]; terminal_updates=[]; new_setups=[]\n    with pg_connect() as c:\n        rows=[dict(r) for r in c.execute("SELECT * FROM trade_setups WHERE status=\'ACTIVE\' ORDER BY updated_at DESC").fetchall()]\n\n    # Collapse legacy horizon-duplicates to one active setup per asset.\n    active_by_asset={}\n    duplicate_ids=[]\n    for st in rows:\n        a=str(st.get(\'asset\'))\n        if a not in active_by_asset:\n            active_by_asset[a]=st\n        else:\n            duplicate_ids.append(st[\'setup_id\'])\n\n    # Manage existing trade states.\n    for asset,st in list(active_by_asset.items()):\n        x=candidates.get(asset)\n        pay=st[\'payload\'] if isinstance(st.get(\'payload\'),dict) else json.loads(st.get(\'payload\') or \'{}\')\n        direction=str(st.get(\'direction\'))\n        px=float((x or {}).get(\'price\') or st.get(\'entry_price\') or 0.0)\n        stop=st.get(\'stop_price\')\n        terminal=None; reason=None\n\n        if stop is not None and px>0 and ((direction==\'LONG\' and px<=float(stop)) or (direction==\'SHORT\' and px>=float(stop))):\n            terminal=\'STOP\'; reason=\'structural_stop_crossed\'\n        elif x:\n            terminal,reason=_uec_hard_terminal(x,direction)\n\n        if terminal:\n            payload={**pay,\'schema_version\':TRADE_ALERT_SCHEMA_VERSION,\'trigger_ts\':now(),\n                     \'action\':terminal+\'_\'+direction,\'trigger_price\':px,\'reason\':reason,\n                     \'canonical_trade_state\':True,\'robot_eligible\':False,\'execution_mode\':\'SHADOW_ONLY\'}\n            alert_rows.append((asset,str(st.get(\'horizon\') or (x or {}).get(\'horizon\') or \'1h\'),terminal,\'high\',payload))\n            terminal_updates.append((terminal,payload,st[\'setup_id\']))\n            active_by_asset.pop(asset,None)\n        elif x:\n            # Refresh support metadata without creating a new trade.\n            payload={**pay,\n                     \'supporting_horizons\':x.get(\'_uec_supporting_horizons\') or [],\n                     \'direction_support\':x.get(\'_uec_direction_support\') or {},\n                     \'last_execution_horizon\':x.get(\'horizon\'),\n                     \'last_seen_at\':now()}\n            terminal_updates.append((\'ACTIVE_REFRESH\',payload,st[\'setup_id\']))\n\n    # Create one new setup per asset after terminals have been resolved.\n    for asset,x in candidates.items():\n        if asset in active_by_asset: continue\n        d=str(x.get(\'research_decision\') or \'NO_TRADE\')\n        if d not in (\'LONG\',\'SHORT\'): continue\n        plan=x.get(\'trade_plan\') or {}\n        # Signal-first: secondary timing/economics affect scale, not existence.\n        ti=plan.get(\'trade_integrity\') or {}\n        ec=plan.get(\'execution_consistency\') or {}\n        arb=plan.get(\'rule_arbitration\') or {}\n        hard=bool(ti.get(\'hard_invalidation\') or ec.get(\'status\')==\'VETO\' or ((arb.get(\'hard_veto\') or {}).get(\'decision\')==\'VETO\'))\n        if hard: continue\n\n        setup_id=str(x.get(\'_uec_setup_id\') or _uec_canonical_setup_id(asset,d,x))\n        h=str(x.get(\'horizon\') or \'1h\')\n        exp=float(plan.get(\'expected_move_pct\') or 0.0)\n        frac=float(plan.get(\'initial_position_fraction\') or 0.0)\n        if frac<=0: frac=0.10\n        frac=clip(frac,0.05,1.0)\n        payload={\'schema_version\':TRADE_ALERT_SCHEMA_VERSION,\'setup_id\':setup_id,\'canonical_trade_state\':True,\n                 \'trigger_ts\':now(),\'asset\':asset,\'horizon\':h,\'execution_horizon\':h,\n                 \'supporting_horizons\':x.get(\'_uec_supporting_horizons\') or [h],\n                 \'direction_support\':x.get(\'_uec_direction_support\') or {},\n                 \'action\':\'ENTRY_\'+d,\'direction\':d,\'trigger_price\':float(x.get(\'price\') or plan.get(\'entry_price\') or 0),\n                 \'stop_price\':plan.get(\'stop_price\'),\'invalidation_price\':plan.get(\'invalidation_price\'),\n                 \'expected_move_pct\':exp,\'expected_move_method\':plan.get(\'expected_move_method\'),\n                 \'signal_tier\':x.get(\'signal_tier\'),\'signal_strength\':x.get(\'confidence\'),\n                 \'entry_quality\':plan.get(\'entry_quality\'),\'initial_position_fraction\':frac,\n                 \'scaling_policy\':plan.get(\'scaling_policy\'),\'expected_to_stop_ratio\':plan.get(\'expected_to_stop_ratio\'),\n                 \'stop_method\':plan.get(\'stop_method\'),\'setup_family\':_uec_setup_family(x),\n                 \'structural_anchor\':_uec_anchor(x,d),\'robot_eligible\':False,\'execution_mode\':\'SHADOW_ONLY\'}\n        alert_rows.append((asset,h,\'ENTRY\',\'medium\',payload))\n        new_setups.append((setup_id,asset,h,d,plan,exp,payload))\n\n    if alert_rows or terminal_updates or new_setups or duplicate_ids:\n        with pg_connect() as c:\n            for dup in duplicate_ids:\n                c.execute("UPDATE trade_setups SET status=\'MERGED_DUPLICATE\',updated_at=%s WHERE setup_id=%s",(now(),dup))\n            for asset,h,atype,sev,payload in alert_rows:\n                out.append(_insert_trade_alert_conn(c,asset,h,atype,sev,payload))\n            for terminal,payload,setup_id in terminal_updates:\n                if terminal==\'ACTIVE_REFRESH\':\n                    c.execute("UPDATE trade_setups SET updated_at=%s,payload=%s::jsonb WHERE setup_id=%s",\n                              (now(),json.dumps(payload,ensure_ascii=False),setup_id))\n                else:\n                    c.execute("UPDATE trade_setups SET status=%s,updated_at=%s,payload=%s::jsonb WHERE setup_id=%s",\n                              (terminal,now(),json.dumps(payload,ensure_ascii=False),setup_id))\n            for setup_id,asset,h,d,plan,exp,payload in new_setups:\n                c.execute("""INSERT INTO trade_setups(setup_id,created_at,updated_at,asset,horizon,direction,status,entry_price,stop_price,invalidation_price,expected_move_pct,payload)\n                             VALUES(%s,%s,%s,%s,%s,%s,\'ACTIVE\',%s,%s,%s,%s,%s::jsonb)\n                             ON CONFLICT(setup_id) DO UPDATE SET updated_at=EXCLUDED.updated_at,payload=EXCLUDED.payload""",\n                          (setup_id,now(),now(),asset,h,d,float(payload.get(\'trigger_price\') or 0),plan.get(\'stop_price\'),\n                           plan.get(\'invalidation_price\'),exp,json.dumps(payload,ensure_ascii=False)))\n    return out\n\ndef _signed_trade_return(direction,entry,price):', 'v80.0 unified canonical trade alert manager'),
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\ndef sync_shadow_trade_lifecycle(summary):\n    """Unified asset-level lifecycle for the canonical trade state."""\n    if not (SHADOW_LIFECYCLE_ENABLED and pg_enabled()): return {\'status\':\'disabled\',\'events\':0}\n    candidates=_uec_asset_candidates(summary)\n    events=0\n    try:\n        with pg_connect() as c:\n            active_setups=[dict(r) for r in c.execute("SELECT * FROM trade_setups WHERE status=\'ACTIVE\' ORDER BY updated_at DESC").fetchall()]\n            active_trades=[dict(r) for r in c.execute("""SELECT t.*,s.status setup_status,s.payload setup_payload\n                                                        FROM shadow_trades t JOIN trade_setups s ON s.setup_id=t.setup_id\n                                                        WHERE t.status=\'ACTIVE\'""").fetchall()]\n            trades_by_setup={r[\'setup_id\']:r for r in active_trades}\n\n            for st in active_setups:\n                if st[\'setup_id\'] in trades_by_setup: continue\n                x=candidates.get(str(st[\'asset\'])) or {}\n                sp=st[\'payload\'] if isinstance(st[\'payload\'],dict) else json.loads(st[\'payload\'] or \'{}\')\n                frac=float(sp.get(\'initial_position_fraction\') or ENTRY_SCALE_EARLY); frac=clip(frac,0.05,1.0)\n                price=float(st[\'entry_price\']); trade_id=\'ST_\'+hashlib.sha256((st[\'setup_id\']+\'|canonical\').encode()).hexdigest()[:24]\n                stage=\'ENTRY\'\n                payload={\'canonical_trade_state\':True,\'execution_horizon\':sp.get(\'execution_horizon\') or st.get(\'horizon\'),\n                         \'supporting_horizons\':sp.get(\'supporting_horizons\') or [],\n                         \'regime_open\':x.get(\'regime\'),\'signal_tier_open\':x.get(\'signal_tier\'),\n                         \'execution_mode\':\'SHADOW_ONLY\',\'path_dependent\':True}\n                c.execute("""INSERT INTO shadow_trades\n                    (trade_id,setup_id,created_at,updated_at,asset,horizon,direction,status,entry_price,avg_entry_price,\n                     initial_fraction,current_fraction,max_fraction,stop_price,high_price,low_price,realized_pnl_fraction,total_pnl_fraction,stage,payload)\n                    VALUES(%s,%s,%s,%s,%s,%s,%s,\'ACTIVE\',%s,%s,%s,%s,%s,%s,%s,%s,0,0,%s,%s::jsonb)\n                    ON CONFLICT(setup_id) DO NOTHING""",\n                    (trade_id,st[\'setup_id\'],now(),now(),st[\'asset\'],st[\'horizon\'],st[\'direction\'],price,price,frac,frac,frac,\n                     st.get(\'stop_price\'),price,price,stage,json.dumps(payload,ensure_ascii=False)))\n                _lifecycle_event_conn(c,trade_id,st[\'setup_id\'],st[\'asset\'],st[\'horizon\'],\'ENTRY\',price,frac,st.get(\'stop_price\'),stage,payload)\n                events+=1\n\n            rows=[dict(r) for r in c.execute("""SELECT t.*,s.status setup_status,s.payload setup_payload,s.stop_price setup_stop\n                                               FROM shadow_trades t JOIN trade_setups s ON s.setup_id=t.setup_id\n                                               WHERE t.status=\'ACTIVE\'""").fetchall()]\n            for tr in rows:\n                x=candidates.get(str(tr[\'asset\'])) or {}\n                setup_payload=tr[\'setup_payload\'] if isinstance(tr[\'setup_payload\'],dict) else json.loads(tr[\'setup_payload\'] or \'{}\')\n\n                if tr[\'setup_status\']!=\'ACTIVE\':\n                    px=float(setup_payload.get(\'trigger_price\') or x.get(\'price\') or tr[\'avg_entry_price\'])\n                    rem=float(tr.get(\'current_fraction\') or 0.0)\n                    realized=float(tr.get(\'realized_pnl_fraction\') or 0.0)+rem*_signed_trade_return(tr[\'direction\'],tr[\'avg_entry_price\'],px)\n                    status=str(tr[\'setup_status\']); stage=\'CLOSED_\'+status\n                    c.execute("""UPDATE shadow_trades SET status=%s,updated_at=%s,closed_at=%s,exit_price=%s,current_fraction=0,\n                                 realized_pnl_fraction=%s,total_pnl_fraction=%s,stage=%s WHERE trade_id=%s""",\n                              (status,now(),now(),px,realized,realized,stage,tr[\'trade_id\']))\n                    _lifecycle_event_conn(c,tr[\'trade_id\'],tr[\'setup_id\'],tr[\'asset\'],tr[\'horizon\'],status,px,0,tr.get(\'stop_price\'),stage,\n                                          {\'terminal_reason\':setup_payload.get(\'reason\'),\'canonical_trade_state\':True})\n                    events+=1\n                    continue\n\n                if not x or x.get(\'price\') is None: continue\n                px=float(x[\'price\']); direction=tr[\'direction\']\n                cur=float(tr.get(\'current_fraction\') or 0.0); avg=float(tr.get(\'avg_entry_price\') or tr[\'entry_price\'])\n                high=max(float(tr.get(\'high_price\') or px),px); low=min(float(tr.get(\'low_price\') or px),px)\n                plan=x.get(\'trade_plan\') or {}\n                stage=str(x.get(\'decision_stage\') or tr.get(\'stage\') or \'HOLD\')\n                desired=clip(float(plan.get(\'initial_position_fraction\') or cur),0.05,1.0)\n\n                # Scale only; soft deterioration no longer forces an independent shadow reduction.\n                if desired>cur+1e-6 and stage in (\'CONFIRMED_SCALE\',\'CONFIRMED_FULL\',\'ENTER_AND_SCALE\',\'ENTER_FULL_CANDIDATE\'):\n                    add=min(1.0-cur,desired-cur)\n                    new_frac=cur+add; new_avg=((avg*cur)+(px*add))/new_frac if new_frac>0 else avg\n                    cur=new_frac; avg=new_avg\n                    c.execute("UPDATE shadow_trades SET add_count=add_count+1 WHERE trade_id=%s",(tr[\'trade_id\'],))\n                    _lifecycle_event_conn(c,tr[\'trade_id\'],tr[\'setup_id\'],tr[\'asset\'],tr[\'horizon\'],\'ADD\',px,add,tr.get(\'stop_price\'),stage,\n                                          {\'target_fraction\':desired,\'canonical_trade_state\':True})\n                    events+=1\n\n                # Structural trailing only: never replace the active stop with a tactical/noise stop.\n                old_stop=tr.get(\'stop_price\'); trail=old_stop\n                new_stop=plan.get(\'stop_price\')\n                structural=bool(plan.get(\'structural_stop_enforced\'))\n                if structural and new_stop is not None:\n                    ns=float(new_stop)\n                    if direction==\'LONG\' and ns<px and (old_stop is None or ns>float(old_stop)):\n                        trail=ns\n                    elif direction==\'SHORT\' and ns>px and (old_stop is None or ns<float(old_stop)):\n                        trail=ns\n                if trail is not None and old_stop is not None and abs(float(trail)-float(old_stop))>1e-9:\n                    _lifecycle_event_conn(c,tr[\'trade_id\'],tr[\'setup_id\'],tr[\'asset\'],tr[\'horizon\'],\'STRUCTURAL_TRAIL\',px,cur,trail,stage,\n                                          {\'old_stop\':old_stop,\'new_stop\':trail,\'canonical_trade_state\':True})\n                    events+=1\n\n                realized=float(tr.get(\'realized_pnl_fraction\') or 0.0)\n                total=realized+cur*_signed_trade_return(direction,avg,px)\n                payload=tr[\'payload\'] if isinstance(tr.get(\'payload\'),dict) else json.loads(tr.get(\'payload\') or \'{}\')\n                payload={**payload,\'supporting_horizons\':x.get(\'_uec_supporting_horizons\') or [],\n                         \'last_execution_horizon\':x.get(\'horizon\'),\'canonical_trade_state\':True}\n                c.execute("""UPDATE shadow_trades SET updated_at=%s,avg_entry_price=%s,current_fraction=%s,\n                             max_fraction=GREATEST(max_fraction,%s),stop_price=%s,high_price=%s,low_price=%s,\n                             total_pnl_fraction=%s,stage=%s,payload=%s::jsonb WHERE trade_id=%s""",\n                          (now(),avg,cur,cur,trail,high,low,total,stage,json.dumps(payload,ensure_ascii=False),tr[\'trade_id\']))\n        return {\'status\':\'ok\',\'events\':events,\'mode\':\'UNIFIED_ASSET_TRADE_STATE\'}\n    except Exception as ex:\n        emit(\'shadow_lifecycle_error\',error=f\'{type(ex).__name__}: {ex}\')\n        return {\'status\':\'error\',\'events\':events,\'error\':f\'{type(ex).__name__}: {ex}\'}\n\ndef trade_lifecycle_board(limit=100):', 'v80.0 unified shadow lifecycle'),
    ("VERSION = 'veritas-max-product-v79.1-signal-first-portfolio-repair'", "VERSION = 'veritas-max-product-v80.0-unified-execution-core'", 'v80.0 runtime version'),

)

PORTFOLIO_PATCHES = (
    ("VERSION='veritas-portfolio-v2-v70.8.4'", "VERSION='veritas-portfolio-v3-v78-impulse'", 'v78 portfolio version'),
    ("POLICIES={\n 'Champion': {'threshold':0.70,'strong_threshold':0.82,'min_independent':3},\n 'Challenger': {'threshold':0.77,'strong_threshold':0.86,'min_independent':4},\n}", "POLICIES={\n 'Champion': {'threshold':0.70,'strong_threshold':0.82,'min_independent':3,'mode':'CORE'},\n 'Challenger': {'threshold':0.77,'strong_threshold':0.86,'min_independent':4,'mode':'CORE'},\n 'Impulse': {\n     'threshold':0.64,'strong_threshold':0.76,'min_independent':2,'mode':'IMPULSE_ONLY',\n     'allowed_horizons':('1h','4h','1d'),'max_fraction':0.50,'provisional_cap':0.10,\n     'accepted_cap':0.25,'confirmed_cap':0.50\n },\n}", 'v78 impulse portfolio policy'),
    ('def _risk_governor(drawdown):', 'def _best_impulse_by_asset(summary):\n    """Independent candidate book: only short/medium impulse trades.\n\n    Admission is based on current impulse/reversal structure. A slow strategic\n    trend by itself is not enough. This intentionally exits when impulse quality\n    disappears, even if the long-horizon thesis remains valid.\n    """\n    out={}\n    for r0 in summary or []:\n        r=dict(r0)\n        h=str(r.get(\'horizon\') or \'\')\n        if h not in (\'1h\',\'4h\',\'1d\'):\n            continue\n        d=str(r.get(\'research_decision\') or \'NO_TRADE\')\n        plan=r.get(\'trade_plan\') or {}\n        gen=r.get(\'impulse_genesis\') or {}\n        piv=r.get(\'impulse_pivot_break\') or {}\n        tr=r.get(\'tactical_reversal\') or {}\n        inst=r.get(\'institutional_signal\') or {}\n        bq=inst.get(\'breakout_quality\') or {}\n        shift=str(plan.get(\'regime_shift_state\') or \'\')\n        ec=plan.get(\'execution_consistency\') or {}\n\n        candidates=[]\n        for x,name,minp in (\n            (gen,\'IMPULSE_GENESIS\',0.68),\n            (piv,\'IMPULSE_PIVOT_BREAK\',0.72),\n            (tr,\'TACTICAL_REVERSAL\',0.72),\n        ):\n            cd=str(x.get(\'direction\') or x.get(\'candidate_direction\') or \'NO_TRADE\')\n            pr=float(x.get(\'probability\') or 0.0)\n            active=bool(x.get(\'active\'))\n            if cd in (\'LONG\',\'SHORT\') and (active or pr>=minp):\n                candidates.append((pr,cd,name,x))\n\n        # Accepted breakout continuation is also an impulse candidate.\n        bdir=str(bq.get(\'direction\') or \'NO_TRADE\')\n        bstate=str(bq.get(\'state\') or \'\')\n        if bdir in (\'LONG\',\'SHORT\') and bstate in (\'EARLY_BREAKOUT\',\'CONFIRMED_BREAKOUT\') and shift in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\'):\n            candidates.append((0.70 if bstate==\'EARLY_BREAKOUT\' else 0.76,bdir,\'BREAKOUT_IMPULSE\',bq))\n\n        if not candidates:\n            continue\n        candidates.sort(key=lambda z:z[0],reverse=True)\n        pr,cd,setup,raw=candidates[0]\n        if d not in (\'LONG\',\'SHORT\'):\n            d=cd\n        if d!=cd:\n            continue\n        if ec.get(\'status\')==\'VETO\':\n            continue\n        if not bool(r.get(\'execution_eligible\')) and not bool(plan.get(\'eligible\')):\n            continue\n        if plan.get(\'eligible\') is False and not raw.get(\'active\'):\n            continue\n\n        p,source=_signal_probability(r)\n        p=max(float(p),float(pr))\n        indep=int(((inst.get(\'evidence_independence\') or {}).get(\'independent_count\')) or 0)\n        rr=float(plan.get(\'expected_to_stop_ratio\') or raw.get(\'reward_risk\') or 0.0)\n        tq=float(plan.get(\'trade_quality_score\') or 0.0)\n\n        score=p + 0.025*min(indep,5) + 0.04*min(1.0,max(0.0,rr/2.0)) + 0.04*tq\n        r[\'research_decision\']=d\n        r[\'_pwin\']=min(0.92,p)\n        r[\'_pwin_source\']=\'IMPULSE_\'+str(source)\n        r[\'_rank\']=score\n        r[\'_impulse_setup\']=setup\n        r[\'_impulse_probability\']=pr\n        a=str(r.get(\'asset\'))\n        if a and (a not in out or score>out[a][\'_rank\']):\n            out[a]=r\n    return out\n\n\ndef _risk_governor(drawdown):', 'v78 impulse candidate book'),
    ("def _desired_fraction(row,policy,drawdown):\n    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')", "def _desired_fraction(row,policy,drawdown):\n    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')\n    mode=str(policy.get('mode') or 'CORE')\n    if mode=='IMPULSE_ONLY':\n        h=str(row.get('horizon') or '')\n        if h not in tuple(policy.get('allowed_horizons') or ('1h','4h','1d')): return 0.0\n        plan=row.get('trade_plan') or {}\n        ec=plan.get('execution_consistency') or {}\n        if ec.get('status')=='VETO': return 0.0\n        if (plan.get('reentry_intelligence') or {}).get('allowed') is False: return 0.0\n        rr=float(plan.get('expected_to_stop_ratio') or 0.0)\n        if rr<0.75: return 0.0\n        shift=str(plan.get('regime_shift_state') or '')\n        setup=str(row.get('_impulse_setup') or plan.get('setup') or '')\n        if not setup: return 0.0\n        stage=str(plan.get('position_stage') or '')\n        base=float(plan.get('initial_position_fraction') or 0.0)\n        if shift in ('NEW_REGIME_PROVISIONAL','TRANSITION','OLD_REGIME_WEAKENING'):\n            f=min(base if base>0 else 0.10,float(policy.get('provisional_cap') or 0.10))\n        elif shift=='NEW_REGIME_ACCEPTED':\n            f=min(max(base,0.15),float(policy.get('accepted_cap') or 0.25))\n            if float(row.get('_impulse_probability') or 0)>=0.80 and rr>=1.20:\n                f=min(float(policy.get('confirmed_cap') or 0.50),max(f,0.35))\n        else:\n            f=min(base if base>0 else 0.10,0.15)\n        if float(plan.get('trade_quality_score') or 0.0)>0 and float(plan.get('trade_quality_score') or 0.0)<0.45:\n            f=min(f,0.05)\n        rg=_risk_governor(drawdown); f*=rg['multiplier']\n        return _clip(_round_step(f),0,float(policy.get('max_fraction') or 0.50))", 'v78 impulse sizing'),
    ('def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):\n    ensure_schema(pg_connect); ts=observed_at or _now(); candidates=_best_by_asset(summary)', 'def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):\n    ensure_schema(pg_connect); ts=observed_at or _now(); candidates=_best_by_asset(summary); impulse_candidates=_best_impulse_by_asset(summary)', 'v78 build impulse candidate set'),
    ("        results=[]\n        for name,pol in POLICIES.items(): results.append(_step_one(c,name,pol,candidates,prices,ruonia,usdrub,ts,commission_rate))\n    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'ruonia_source':rusrc,'usdrub_source':fxsrc,'objective_order':['WIN_RATE','TOTAL_RETURN','DRAWDOWN'],'meaningful_win_threshold_nav':MEANINGFUL_WIN_NAV,'admission_probability_floor':{'Champion':0.70,'Challenger':0.77},", "        results=[]\n        for name,pol in POLICIES.items():\n            book=impulse_candidates if str(pol.get('mode') or '')=='IMPULSE_ONLY' else candidates\n            results.append(_step_one(c,name,pol,book,prices,ruonia,usdrub,ts,commission_rate))\n    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'impulse_candidates':len(impulse_candidates),'ruonia_source':rusrc,'usdrub_source':fxsrc,'objective_order':['WIN_RATE','TOTAL_RETURN','DRAWDOWN'],'meaningful_win_threshold_nav':MEANINGFUL_WIN_NAV,'admission_probability_floor':{'Champion':0.70,'Challenger':0.77,'Impulse':0.64},", 'v78 route impulse portfolio'),

    # ----- v78.1 Rule & Experience Arbitration -----
    ('\n\ndef trade_lifecycle_board(limit=100):', '\n\n\n# ---------------- v78.1 Rule & Experience Arbitration ----------------\n\nRULE_HIERARCHY = {\n    \'HARD_SAFETY_GATE\':100,          # kill/source/time/data/thesis hard veto\n    \'THESIS_INVALIDATION\':98,\n    \'STRUCTURAL_RISK\':92,            # structural stop / portfolio risk / max loss\n    \'REENTRY_GOVERNANCE\':90,\n    \'VALIDATED_STATISTICS\':85,       # clean OOS/live evidence; EP23\n    \'EXPERT_FOUNDATIONAL\':80,        # explicit expert/user principles\n    \'REGIME_HORIZON\':72,\n    \'SETUP_STRUCTURAL\':68,\n    \'TACTICAL_EXECUTION\':60,\n    \'SHADOW_KNOWLEDGE\':45,\n    \'EXPLORATORY_HYPOTHESIS\':30,\n}\n\n_EVIDENCE_RANK={\'A\':5,\'B\':4,\'C\':3,\'D\':2,\'E\':1}\n\ndef _knowledge_rule_rank(rule, source=None):\n    """Deterministic seniority for knowledge rules.\n\n    Newer code/rules do not win merely because they were added later.\n    A rule may displace a senior rule only through an explicit supersedes\n    relationship plus validation.\n    """\n    status=str(rule.get(\'status\') or \'\')\n    action=str(rule.get(\'action\') or \'\')\n    agent=str(rule.get(\'agent\') or \'\')\n    source=source or {}\n    grade=str(source.get(\'evidence_grade\') or \'E\').upper()\n\n    if action in (\'RISK_REDUCE\',\'NO_TRADE\') or agent==\'RISK\':\n        tier=RULE_HIERARCHY[\'STRUCTURAL_RISK\']\n    elif status==\'validated_candidate\':\n        tier=RULE_HIERARCHY[\'VALIDATED_STATISTICS\']\n    else:\n        tier=RULE_HIERARCHY[\'SHADOW_KNOWLEDGE\']\n\n    evidence=_EVIDENCE_RANK.get(grade,0)\n    prior=float(rule.get(\'prior_weight\') or 0.0)\n\n    # Explicit scope specificity is a tiebreaker, never enough to beat a higher tier.\n    assets=rule.get(\'asset_scope\') or []\n    horizons=rule.get(\'horizons\') or []\n    specificity=(1 if len(assets)==1 else 0)+(1 if len(horizons)==1 else 0)\n\n    return (tier,evidence,specificity,prior,str(rule.get(\'rule_id\') or \'\'))\n\ndef arbitrate_knowledge_conflicts(kmatches, asset, horizon):\n    """Resolve opposing matched knowledge rules before they reach scoring.\n\n    Same-direction evidence may coexist. For LONG-vs-SHORT conflicts, the\n    highest seniority rule wins. Exact ties fail closed to neutral knowledge.\n    Risk-reduction / NO_TRADE rules are retained independently and can never\n    be overruled by a lower directional rule.\n    """\n    if not kmatches:\n        return {\'selected_rules\':[],\'suppressed_rules\':[],\'conflicts\':[],\n                \'status\':\'NO_RULES\',\'policy\':\'seniority_first\'}\n\n    _, rules=all_knowledge()\n    rule_map={str(r.get(\'rule_id\')):r for r in rules}\n    sources,_rules_unused=all_knowledge()\n    source_map={str(s.get(\'source_id\')):s for s in sources if isinstance(s,dict)}\n\n    enriched=[]\n    for m in kmatches:\n        r=rule_map.get(str(m.get(\'rule_id\'))) or dict(m)\n        src=source_map.get(str(r.get(\'source_id\'))) or {}\n        rank=_knowledge_rule_rank(r,src)\n        x=dict(m)\n        x[\'_rank\']=rank\n        x[\'_tier\']=rank[0]\n        x[\'_evidence_rank\']=rank[1]\n        x[\'_rule_status\']=r.get(\'status\')\n        x[\'_evidence_grade\']=src.get(\'evidence_grade\')\n        enriched.append(x)\n\n    risk=[x for x in enriched if x.get(\'action\') in (\'RISK_REDUCE\',\'NO_TRADE\') or str(x.get(\'agent\'))==\'RISK\']\n    long=[x for x in enriched if x.get(\'action\')==\'LONG\']\n    short=[x for x in enriched if x.get(\'action\')==\'SHORT\']\n    neutral=[x for x in enriched if x not in risk+long+short]\n\n    selected=list(risk)+list(neutral)\n    suppressed=[]\n    conflicts=[]\n\n    if long and short:\n        best_long=max(long,key=lambda x:x[\'_rank\'])\n        best_short=max(short,key=lambda x:x[\'_rank\'])\n        conflicts.append({\n            \'type\':\'DIRECTIONAL_RULE_CONFLICT\',\n            \'long_rule\':best_long.get(\'rule_id\'),\'long_rank\':best_long[\'_rank\'][:-1],\n            \'short_rule\':best_short.get(\'rule_id\'),\'short_rank\':best_short[\'_rank\'][:-1],\n        })\n        # Compare semantic rank without lexical rule_id tiebreaker.\n        lr=best_long[\'_rank\'][:-1]; sr=best_short[\'_rank\'][:-1]\n        if lr>sr:\n            selected.extend(long)\n            suppressed.extend(short)\n            resolution=\'LONG_HIGHER_SENIORITY\'\n        elif sr>lr:\n            selected.extend(short)\n            suppressed.extend(long)\n            resolution=\'SHORT_HIGHER_SENIORITY\'\n        else:\n            # Equal seniority = insufficient basis to choose. Fail closed.\n            suppressed.extend(long+short)\n            resolution=\'EQUAL_SENIORITY_NEUTRALIZED\'\n        conflicts[-1][\'resolution\']=resolution\n    else:\n        selected.extend(long+short)\n\n    def clean(x):\n        z={k:v for k,v in x.items() if not k.startswith(\'_\')}\n        z[\'rule_tier\']=x.get(\'_tier\')\n        z[\'evidence_rank\']=x.get(\'_evidence_rank\')\n        return z\n\n    return {\n        \'status\':\'CONFLICT_RESOLVED\' if conflicts else \'PASS\',\n        \'selected_rules\':[clean(x) for x in selected],\n        \'suppressed_rules\':[clean(x) for x in suppressed],\n        \'conflicts\':conflicts,\n        \'policy\':\'hard/risk > validated statistics > foundational expert > regime/structural setup > tactical > shadow; equal seniority fails closed\',\n        \'new_rule_override_policy\':\'NO_IMPLICIT_OVERRIDE; explicit supersedes + validation required\',\n    }\n\ndef system_rule_arbitration(asset,horizon,f,plan,research_dec):\n    """Final system-wide hierarchy for rule/experience conflicts."""\n    events=[]\n    direction=str(research_dec or plan.get(\'direction\') or \'NO_TRADE\')\n    shift=str(plan.get(\'regime_shift_state\') or \'\')\n    reentry=plan.get(\'reentry_intelligence\') or {}\n    prof=plan.get(\'profitability_gate\') or {}\n    st=f.get(\'intraday_structure\') or {}\n\n    def add(rule_id,tier,active,decision,reason):\n        if active:\n            events.append({\'rule_id\':rule_id,\'tier\':tier,\'decision\':decision,\'reason\':reason})\n\n    add(\'HARD_THESIS_VETO\',RULE_HIERARCHY[\'THESIS_INVALIDATION\'],\n        str(f.get(\'v70_thesis_status\') or \'\') in (\'BROKEN\',\'INVALIDATED\') or str(f.get(\'v70_gate_class\') or \'\')==\'THESIS_VETO\',\n        \'VETO\',\'Full thesis invalidation dominates all entry rules.\')\n\n    add(\'SOURCE_TIME_KILL_GATE\',RULE_HIERARCHY[\'HARD_SAFETY_GATE\'],\n        (not bool(f.get(\'source_gate_pass\',True))) or\n        (not bool(f.get(\'market_open\',True)) and asset not in CRYPTO_ASSETS) or\n        runtime_bool(\'kill_switch\',KILL_SWITCH),\n        \'VETO\',\'Data/source/time/kill gates are absolute.\')\n\n    add(\'STRUCTURAL_STOP_RULE\',RULE_HIERARCHY[\'STRUCTURAL_RISK\'],\n        bool(plan.get(\'structural_stop_enforced\')),\n        \'KEEP\',\'Structural invalidation has priority over tactical stop convenience.\')\n\n    add(\'REENTRY_BLOCK\',RULE_HIERARCHY[\'REENTRY_GOVERNANCE\'],\n        reentry.get(\'allowed\') is False,\n        \'VETO\',\'No repeat trade without a new structural event.\')\n\n    add(\'NEGATIVE_VALIDATED_SETUP_EDGE\',RULE_HIERARCHY[\'VALIDATED_STATISTICS\'],\n        str(prof.get(\'status\') or \'\')==\'NEGATIVE_EDGE\' or\n        (prof.get(\'allow\') is False and str((prof.get(\'profile\') or {}).get(\'status\') or \'\')==\'NEGATIVE_EDGE\'),\n        \'VETO\',\'Validated negative setup expectancy dominates tactical enthusiasm.\')\n\n    add(\'NEW_SETUP_RESETS_OLD_ENTRY_FAILURE\',RULE_HIERARCHY[\'SETUP_STRUCTURAL\'],\n        shift in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\') and\n        str(st.get(\'lifecycle\') or \'\')==\'FAILURE\' and\n        bool((plan.get(\'v77\') or {}).get(\'old_failure_reset\')),\n        \'ALLOW_NEW_SETUP\',\'Old entry failure belongs to the previous setup, not the new structural event.\')\n\n    # Sort only by semantic tier; higher tier always wins.\n    events.sort(key=lambda x:x[\'tier\'],reverse=True)\n    winner=events[0] if events else None\n\n    # Hard veto at a higher tier cannot be cancelled by lower allow/keep rules.\n    hard_veto=next((x for x in events if x[\'decision\']==\'VETO\'),None)\n    if hard_veto:\n        plan[\'eligible\']=False\n        plan[\'initial_position_fraction\']=0.0\n        plan[\'reason\']=\'rule_arbitration_veto:\'+hard_veto[\'rule_id\']\n\n    plan[\'rule_arbitration\']={\n        \'status\':\'CONFLICTS_RESOLVED\' if len(events)>1 else (\'PASS\' if events else \'NO_CONFLICT\'),\n        \'winner\':winner,\n        \'hard_veto\':hard_veto,\n        \'active_rules\':events,\n        \'hierarchy\':RULE_HIERARCHY,\n        \'policy\':\'Higher semantic seniority wins. Newer/younger rule never overrides a senior rule unless explicit supersedes is validated.\',\n    }\n    return plan\n\ndef trade_lifecycle_board(limit=100):', 'v78.1 rule experience arbitration engine'),
    ("                kmatches = match_knowledge(asset, horizon, f, deriv)\n                orth_evidence = orthogonal_knowledge_summary(kmatches)\n                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])", "                kmatches = match_knowledge(asset, horizon, f, deriv)\n                knowledge_arbitration = arbitrate_knowledge_conflicts(kmatches,asset,horizon)\n                kmatches = knowledge_arbitration.get('selected_rules') or []\n                f['knowledge_rule_arbitration']=knowledge_arbitration\n                orth_evidence = orthogonal_knowledge_summary(kmatches)\n                knowledge_adjustment = validated_knowledge_adjustment(kmatches,asset,horizon,f['regime'])", 'v78.1 knowledge conflict arbitration integration'),
    ("                trade_plan=execution_consistency_layer(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", "                trade_plan=execution_consistency_layer(asset,horizon,f,trade_plan,research_dec)\n                trade_plan=system_rule_arbitration(asset,horizon,f,trade_plan,research_dec)\n                trade_plan['institutional_signal']=institutional_signal", 'v78.1 system rule arbitration integration'),
    ("VERSION = 'veritas-max-product-v78.0-impulse-portfolio-consistency'", "VERSION = 'veritas-max-product-v78.1-rule-experience-arbitration'", 'v78.1 runtime version'),

    # ----- v79.0 Portfolio Trade Integrity -----
    ("VERSION='veritas-portfolio-v3-v78-impulse'", "VERSION='veritas-portfolio-v4-v79-trade-integrity'", 'v79 portfolio version'),
    ('def _risk_governor(drawdown):', '\ndef _ti(row):\n    return ((row or {}).get(\'trade_plan\') or {}).get(\'trade_integrity\') or {}\n\ndef _position_payload(z):\n    p=z.get(\'payload\') if isinstance(z,dict) else z[\'payload\']\n    if isinstance(p,dict): return dict(p)\n    try: return json.loads(p or \'{}\')\n    except Exception: return {}\n\ndef _write_position_payload(c,name,asset,payload):\n    c.execute(\'UPDATE paper_positions SET payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s\',\n              (json.dumps(payload),name,asset))\n\ndef _soft_failure_state(c,name,z,row,target,current_frac):\n    """Two-step persistence for soft signal deterioration.\n\n    Hard stop/thesis failure remains immediate. A single missing candidate,\n    confidence fade or timing invalidation only marks one soft failure cycle.\n    """\n    pl=_position_payload(dict(z))\n    n=int(pl.get(\'soft_invalidation_count\') or 0)\n    ti=_ti(row)\n    hard=bool(ti.get(\'hard_invalidation\'))\n    if hard:\n        return {\'hard\':True,\'confirmed_soft\':False,\'count\':n,\'reason\':\'HARD_INVALIDATION\'}\n\n    # Opposite direction is hard only if the opposite row itself is executable.\n    opposite=bool(row and row.get(\'research_decision\') in (\'LONG\',\'SHORT\') and row.get(\'research_decision\')!=z[\'direction\'])\n    opposite_enter=opposite and ti.get(\'entry_permission\')==\'ENTER\'\n    if opposite_enter:\n        return {\'hard\':True,\'confirmed_soft\':False,\'count\':n,\'reason\':\'CONFIRMED_DIRECTION_FLIP\'}\n\n    deteriorated=(row is None) or target<current_frac-0.025 or ti.get(\'entry_permission\') in (\'WAIT_ENTRY\',\'NO_DIRECTION\')\n    if deteriorated:\n        n+=1\n        pl[\'soft_invalidation_count\']=n\n        pl[\'last_soft_invalidation_reason\']=\'candidate_missing_or_timing_deterioration\'\n        _write_position_payload(c,name,z[\'asset\'],pl)\n        return {\'hard\':False,\'confirmed_soft\':n>=2,\'count\':n,\'reason\':\'SOFT_INVALIDATION_PERSISTED\' if n>=2 else \'SOFT_INVALIDATION_FIRST_CYCLE\'}\n\n    if n:\n        pl[\'soft_invalidation_count\']=0\n        pl[\'last_soft_invalidation_reason\']=None\n        _write_position_payload(c,name,z[\'asset\'],pl)\n    return {\'hard\':False,\'confirmed_soft\':False,\'count\':0,\'reason\':\'HEALTHY\'}\n\n\ndef _risk_governor(drawdown):', 'v79 portfolio trade integrity helpers'),
    ("    plan=row.get('trade_plan') or {}\n    rr=float(plan.get('expected_to_stop_ratio') or tr.get('reward_risk') or 0.0)", "    plan=row.get('trade_plan') or {}\n    ti=plan.get('trade_integrity') or {}\n    ec=plan.get('execution_consistency') or {}\n    if ti.get('entry_permission') not in (None,'ENTER'): return 0.0\n    if ti.get('hard_invalidation'): return 0.0\n    if ec.get('status')=='VETO': return 0.0\n    if (plan.get('reentry_intelligence') or {}).get('allowed') is False: return 0.0\n    rr=float(plan.get('expected_to_stop_ratio') or tr.get('reward_risk') or 0.0)", 'v79 fresh entry permission gate'),
    ("    # Assets without qualifying signal target zero -> dynamic exit.\n    for z in pos:\n        if z['asset'] not in targets: targets[z['asset']]=0.0", "    # Missing candidate is NOT an immediate exit anymore.\n    # Keep the current fraction for the first soft-deterioration cycle; the\n    # close/reduce loop below decides whether invalidation is hard or persistent.\n    for z in pos:\n        if z['asset'] not in targets:\n            px=float(prices.get(z['asset'],z['last_price']))\n            targets[z['asset']]=abs(float(z['units'])*px)/max(nav,1.0)", 'v79 no immediate zero target on candidate fade'),
    ("        wrong_dir=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])\n        # stop has priority\n        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))\n        if wrong_dir or stop_hit: target=0.0\n        current_frac=abs(float(z['units'])*px)/max(nav,1.0)\n        if target<current_frac-0.025: _close_or_reduce(c,p,name,z,px,target,nav,ts,'STOP' if stop_hit else 'SIGNAL_REDUCTION')", "        wrong_dir=bool(row and row.get('research_decision') in ('LONG','SHORT') and row.get('research_decision')!=z['direction'])\n        # Structural stop remains an immediate hard invalidation.\n        stop=z['stop_price']; stop_hit=bool(stop is not None and ((z['direction']=='LONG' and px<=float(stop)) or (z['direction']=='SHORT' and px>=float(stop))))\n        current_frac=abs(float(z['units'])*px)/max(nav,1.0)\n        raw_target=float(_desired_fraction(row,policy,dd)) if row else 0.0\n        fs=_soft_failure_state(c,name,z,row,raw_target,current_frac)\n        if stop_hit:\n            _close_or_reduce(c,p,name,z,px,0.0,nav,ts,'STRUCTURAL_STOP')\n        elif fs['hard']:\n            _close_or_reduce(c,p,name,z,px,0.0,nav,ts,fs['reason'])\n        elif fs['confirmed_soft']:\n            # Soft deterioration has persisted across two portfolio cycles.\n            _close_or_reduce(c,p,name,z,px,raw_target,nav,ts,'SOFT_INVALIDATION_CONFIRMED')\n        else:\n            # First soft failure: preserve the position. Do not churn.\n            targets[z['asset']]=current_frac", 'v79 hard soft invalidation persistence'),
    ("        c.execute('UPDATE paper_positions SET units=%s,avg_entry_price=%s,last_price=%s,target_fraction=%s,stop_price=%s,updated_at=%s,payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s',(old_units+units,avg,price,target_fraction,(row.get('trade_plan') or {}).get('stop_price'),ts,json.dumps({'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],'horizon':row.get('horizon'),'signal':(row.get('institutional_signal') or {}).get('investor_signal')}),name,asset))", "        # Preserve the original structural stop/setup on adds. An add is not\n        # permission to silently switch the active trade to another horizon's stop.\n        old_payload=_position_payload(dict(z))\n        old_payload.update({'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],\n                            'last_signal_horizon':row.get('horizon'),\n                            'signal':(row.get('institutional_signal') or {}).get('investor_signal'),\n                            'soft_invalidation_count':0})\n        c.execute('UPDATE paper_positions SET units=%s,avg_entry_price=%s,last_price=%s,target_fraction=%s,updated_at=%s,payload=%s::jsonb WHERE portfolio_name=%s AND asset=%s',\n                  (old_units+units,avg,price,target_fraction,ts,json.dumps(old_payload),name,asset))", 'v79 preserve active structural stop on add'),
    ("        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],'independent':((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),'model_version':VERSION}", "        plan=row.get('trade_plan') or {}\n        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],\n                 'independent':((row.get('institutional_signal') or {}).get('evidence_independence') or {}).get('independent_count'),\n                 'model_version':VERSION,'setup_id':plan.get('setup_id'),'execution_horizon':row.get('horizon'),\n                 'structural_stop_enforced':bool(plan.get('structural_stop_enforced')),\n                 'soft_invalidation_count':0,'entry_permission':(plan.get('trade_integrity') or {}).get('entry_permission')}", 'v79 persist setup identity and execution horizon'),

    # ----- v79.1 Signal-First Portfolio Repair -----
    ("VERSION='veritas-portfolio-v4-v79-trade-integrity'", "VERSION='veritas-portfolio-v5-v79.1-signal-first-repair'", 'v79.1 portfolio version'),
    ('def _risk_governor(drawdown):', '\ndef _signal_first_admission(row, policy, drawdown):\n    """Signal-first portfolio admission.\n\n    Any directional system signal opens a small probe unless a hard safety\n    veto exists. Secondary factors control scale, not existence of the trade.\n    """\n    if not row:\n        return {\'open\':False,\'fraction\':0.0,\'reason\':\'NO_ROW\'}\n    d=str(row.get(\'research_decision\') or \'NO_TRADE\')\n    if d not in (\'LONG\',\'SHORT\'):\n        return {\'open\':False,\'fraction\':0.0,\'reason\':\'NO_DIRECTION\'}\n\n    if not bool(row.get(\'source_gate_pass\',True)):\n        return {\'open\':False,\'fraction\':0.0,\'reason\':\'SOURCE_GATE\'}\n    if not bool(row.get(\'market_open\',True)):\n        return {\'open\':False,\'fraction\':0.0,\'reason\':\'MARKET_CLOSED\'}\n\n    plan=row.get(\'trade_plan\') or {}\n    ti=plan.get(\'trade_integrity\') or {}\n    ec=plan.get(\'execution_consistency\') or {}\n    arb=plan.get(\'rule_arbitration\') or {}\n\n    hard=bool(\n        ti.get(\'hard_invalidation\')\n        or ec.get(\'status\')==\'VETO\'\n        or ((arb.get(\'hard_veto\') or {}).get(\'decision\')==\'VETO\')\n        or (plan.get(\'reentry_intelligence\') or {}).get(\'allowed\') is False\n    )\n    if hard:\n        return {\'open\':False,\'fraction\':0.0,\'reason\':\'HARD_VETO\'}\n\n    # Baseline probe: signal itself creates exposure.\n    mode=str(policy.get(\'mode\') or \'CORE\')\n    base=0.10 if mode==\'CORE\' else 0.05\n\n    # Signal quality scales upward from the probe.\n    p=float(row.get(\'_pwin\') or 0.50)\n    indep=int((((row.get(\'institutional_signal\') or {}).get(\'evidence_independence\') or {}).get(\'independent_count\')) or 0)\n    rr=float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n    tq=float(plan.get(\'trade_quality_score\') or 0.0)\n    action=str((row.get(\'institutional_signal\') or {}).get(\'action\') or \'\')\n    shift=str(plan.get(\'regime_shift_state\') or \'\')\n\n    f=base\n    if p>=0.65: f=max(f,0.15)\n    if p>=0.72 and indep>=2: f=max(f,0.25)\n    if p>=0.78 and indep>=3 and rr>=1.0: f=max(f,0.35)\n    if p>=0.82 and indep>=4 and rr>=1.2: f=max(f,0.50)\n    if action in (\'ENTER_AND_SCALE\',\'ENTER_FULL_CANDIDATE\') and p>=0.82 and rr>=1.2:\n        f=max(f,0.50)\n\n    # Provisional/poor timing reduces size but does not eliminate the trade.\n    if ti.get(\'entry_permission\')==\'WAIT_ENTRY\':\n        f=min(f,0.05)\n    if shift in (\'NEW_REGIME_PROVISIONAL\',\'TRANSITION\',\'OLD_REGIME_WEAKENING\'):\n        f=min(f,0.10)\n    if tq>0 and tq<0.45:\n        f=min(f,0.05)\n    if rr>0 and rr<0.60:\n        f=min(f,0.05)\n\n    # Stop-risk controls size, never invents a closer stop.\n    inst=row.get(\'institutional_signal\') or {}\n    risk_pct=inst.get(\'risk_pct\')\n    if risk_pct is not None:\n        try:\n            rp=float(risk_pct)\n            if rp>0:\n                f=min(f,MAX_STOP_RISK_NAV/rp)\n        except Exception:\n            pass\n\n    rg=_risk_governor(drawdown)\n    f*=rg[\'multiplier\']\n    maxf=float(policy.get(\'max_fraction\') or 2.0)\n    f=_clip(_round_step(f),0,maxf)\n    return {\'open\':f>0,\'fraction\':f,\'reason\':\'SIGNAL_FIRST\',\n            \'pwin\':p,\'independent\':indep,\'rr\':rr,\'trade_quality\':tq,\n            \'entry_permission\':ti.get(\'entry_permission\')}\n\ndef _candidate_book_signal_first(summary):\n    """Choose the strongest directional signal for each asset before sizing.\n\n    Crucially, candidate selection no longer discards an asset because the\n    best-ranked horizon later fails a secondary admission threshold.\n    """\n    out={}\n    for r0 in summary or []:\n        r=dict(r0)\n        d=str(r.get(\'research_decision\') or \'NO_TRADE\')\n        tr=r.get(\'tactical_reversal\') or {}\n        if tr.get(\'active\') and tr.get(\'direction\') in (\'LONG\',\'SHORT\'):\n            d=str(tr.get(\'direction\')); r[\'research_decision\']=d\n        if d not in (\'LONG\',\'SHORT\'):\n            continue\n        if not bool(r.get(\'source_gate_pass\',True)):\n            continue\n        if not bool(r.get(\'market_open\',True)):\n            continue\n\n        p,source=_signal_probability(r)\n        inst=r.get(\'institutional_signal\') or {}\n        ev=inst.get(\'evidence_independence\') or {}\n        bq=inst.get(\'breakout_quality\') or {}\n        plan=r.get(\'trade_plan\') or {}\n        indep=int(ev.get(\'independent_count\') or 0)\n        q=float(bq.get(\'quality_score\') or 0.0)\n        rr=float(plan.get(\'expected_to_stop_ratio\') or 0.0)\n        tq=float(plan.get(\'trade_quality_score\') or 0.0)\n\n        # Direction first, then quality rank.\n        rank=float(p)+0.02*min(indep,6)+0.03*q+0.02*min(max(rr,0.0),2.0)+0.03*tq\n        r[\'_pwin\']=p\n        r[\'_pwin_source\']=source\n        r[\'_rank\']=rank\n        r[\'_signal_first\']=True\n\n        a=str(r.get(\'asset\') or \'\')\n        if a and (a not in out or rank>out[a][\'_rank\']):\n            out[a]=r\n    return out\n\ndef _risk_governor(drawdown):', 'v79.1 signal first helpers'),
    ("def _desired_fraction(row,policy,drawdown):\n    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')", "def _desired_fraction(row,policy,drawdown):\n    sf=_signal_first_admission(row,policy,drawdown)\n    if not sf.get('open'): return 0.0\n    mode=str(policy.get('mode') or 'CORE')\n    if mode=='IMPULSE_ONLY':\n        # Impulse book remains impulse-specific, but any qualified impulse signal gets a probe.\n        h=str(row.get('horizon') or '')\n        if h not in tuple(policy.get('allowed_horizons') or ('1h','4h','1d')): return 0.0\n        if not (row.get('_impulse_setup') or\n                (row.get('impulse_genesis') or {}).get('active') or\n                (row.get('impulse_pivot_break') or {}).get('active') or\n                (row.get('tactical_reversal') or {}).get('active')):\n            return 0.0\n        return min(float(sf['fraction']),float(policy.get('max_fraction') or 0.50))\n    # Core books: signal creates position, secondary factors only scale it.\n    return float(sf['fraction'])\n\ndef _legacy_desired_fraction_unused(row,policy,drawdown):\n    p=float(row['_pwin']); inst=row.get('institutional_signal') or {}; sig=str(inst.get('investor_signal') or '')", 'v79.1 signal first desired fraction'),
    ('def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):\n    ensure_schema(pg_connect); ts=observed_at or _now(); candidates=_best_by_asset(summary); impulse_candidates=_best_impulse_by_asset(summary)', 'def step_all(summary,pg_connect,model_version,observed_at=None,commission_rate=COMMISSION,emit=None):\n    ensure_schema(pg_connect); ts=observed_at or _now()\n    candidates=_candidate_book_signal_first(summary)\n    impulse_candidates=_best_impulse_by_asset(summary)', 'v79.1 signal first candidate routing'),
    ("    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'impulse_candidates':len(impulse_candidates),", "    out={'status':'OK','version':VERSION,'portfolios':results,'market_candidates':len(candidates),'impulse_candidates':len(impulse_candidates),\n         'signal_first_policy':True,'signal_first_probe_fraction_core':0.10,'signal_first_probe_fraction_impulse':0.05,", 'v79.1 portfolio output metadata'),

    # ----- v80.0 Portfolio Unified Execution -----
    ("VERSION='veritas-portfolio-v5-v79.1-signal-first-repair'", "VERSION='veritas-portfolio-v6-v80-unified-execution'", 'v80 portfolio version'),
    ('def _risk_governor(drawdown):', '\ndef _portfolio_canonical_setup_id(row):\n    d=str((row or {}).get(\'research_decision\') or \'NO_TRADE\')\n    if d not in (\'LONG\',\'SHORT\'): return None\n    plan=(row or {}).get(\'trade_plan\') or {}\n    piv=(row or {}).get(\'impulse_pivot_break\') or {}\n    rev=(row or {}).get(\'tactical_reversal\') or {}\n    rng=(row or {}).get(\'range_retest_breakout\') or {}\n    bq=((row or {}).get(\'institutional_signal\') or {}).get(\'breakout_quality\') or {}\n    if piv.get(\'active\'): family=\'IMPULSE_PIVOT_BREAK\'\n    elif rev.get(\'active\'): family=\'TACTICAL_REVERSAL\'\n    elif rng.get(\'active\'): family=\'RANGE_RETEST_BREAKOUT\'\n    elif str(bq.get(\'state\') or \'\') in (\'EARLY_BREAKOUT\',\'CONFIRMED_BREAKOUT\'): family=\'BREAKOUT\'\n    elif str(plan.get(\'regime_shift_state\') or \'\') in (\'NEW_REGIME_PROVISIONAL\',\'NEW_REGIME_ACCEPTED\'): family=\'REGIME_SHIFT\'\n    else: family=\'TREND\'\n    vals=[plan.get(\'breakout_level\'),plan.get(\'recent_swing_anchor\'),\n          ((row or {}).get(\'structural_levels\') or {}).get(\'resistance\' if d==\'LONG\' else \'support\'),\n          plan.get(\'invalidation_price\'),(row or {}).get(\'price\')]\n    anchor=0.0\n    for v in vals:\n        try:\n            if v is not None and float(v)>0:\n                anchor=float(v); break\n        except Exception: pass\n    akey=f\'{anchor:.4g}\' if anchor>0 else \'na\'\n    raw=f"{(row or {}).get(\'asset\')}|{d}|{family}|{akey}"\n    return \'UTS_\'+hashlib.sha256(raw.encode()).hexdigest()[:20]\n\ndef _portfolio_admission_trace(candidates,policy,drawdown):\n    out=[]\n    for asset,row in sorted((candidates or {}).items()):\n        sf=_signal_first_admission(row,policy,drawdown)\n        plan=row.get(\'trade_plan\') or {}\n        out.append({\'asset\':asset,\'direction\':row.get(\'research_decision\'),\'horizon\':row.get(\'horizon\'),\n                    \'canonical_setup_id\':_portfolio_canonical_setup_id(row),\n                    \'pwin\':row.get(\'_pwin\'),\'rank\':row.get(\'_rank\'),\n                    \'rr\':plan.get(\'expected_to_stop_ratio\'),\n                    \'hard_veto\':not bool(sf.get(\'open\')),\n                    \'target_fraction\':sf.get(\'fraction\'),\'reason\':sf.get(\'reason\')})\n    return out\n\ndef _risk_governor(drawdown):', 'v80 portfolio canonical setup helpers'),
    ("        plan=row.get('trade_plan') or {}\n        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],", "        plan=row.get('trade_plan') or {}\n        canonical_setup_id=_portfolio_canonical_setup_id(row)\n        payload={'entry_nav_rub':nav,'pwin':row['_pwin'],'pwin_source':row['_pwin_source'],\n                 'canonical_setup_id':canonical_setup_id,", 'v80 persist canonical setup in paper trade'),
    ("    st=_stats(c,name)\n    return {'name':name,'nav_rub':round(nav,2),", "    st=_stats(c,name)\n    trace=_portfolio_admission_trace(candidates,policy,dd)\n    return {'name':name,'nav_rub':round(nav,2),", 'v80 compute admission trace'),
    ("'cash_equivalent_fraction':round(max(0,1-gross),4),'risk_governor':rg,'ruonia':ruonia,'usdrub':usdrub,**st}", "'cash_equivalent_fraction':round(max(0,1-gross),4),'risk_governor':rg,'ruonia':ruonia,'usdrub':usdrub,\n            'admission_trace':trace,**st}", 'v80 return admission trace'),
    ("    if emit: emit('paper_portfolio_cycle',portfolios=results,market_candidates=len(candidates),ruonia=ruonia,usdrub=usdrub)", "    if emit: emit('paper_portfolio_cycle',portfolios=results,market_candidates=len(candidates),\n                           impulse_candidates=len(impulse_candidates),ruonia=ruonia,usdrub=usdrub,\n                           unified_execution=True)", 'v80 portfolio unified telemetry'),
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
            elif label=='v74.0 runtime version' and "veritas-max-product-v80.0-unified-execution-core" in dst:
                already.append(label+" (superseded by v80)")
            elif label=='v74.0 profitability gate integration' and "trade_path_intelligence(asset,horizon,research_dec,trade_plan)" in dst:
                already.append(label+" (superseded by v76+)")
            elif label=='v75.0 validation integration' and "'trade_path_intelligence':trade_path_intelligence_board()" in dst:
                already.append(label+" (superseded by v76+)")
            elif label=='v76.0 path intelligence integration' and "v77_decision_quality_stack(asset,horizon,f,trade_plan,research_dec)" in dst:
                already.append(label+" (superseded by v77)")
            elif label=='v75.0 runtime version' and ("veritas-max-product-v80.0-unified-execution-core" in dst or "veritas-max-product-v80.0-unified-execution-core" in dst):
                already.append(label+" (superseded)")
            elif label=='v77.0 runtime version' and ("veritas-max-product-v80.0-unified-execution-core" in dst or "veritas-max-product-v80.0-unified-execution-core" in dst):
                already.append(label+" (superseded by v78+)")
            elif label=='v78.0 runtime version' and ("veritas-max-product-v80.0-unified-execution-core" in dst or "veritas-max-product-v80.0-unified-execution-core" in dst):
                already.append(label+" (superseded by v78.1+)")
            elif label=='v78.1 runtime version' and ("veritas-max-product-v80.0-unified-execution-core" in dst or "veritas-max-product-v80.0-unified-execution-core" in dst):
                already.append(label+" (superseded by v79+)")
            elif label=='v79.0 runtime version' and ("veritas-max-product-v80.0-unified-execution-core" in dst or "veritas-max-product-v80.0-unified-execution-core" in dst):
                already.append(label+" (superseded by v79.1+)")
            elif label=='v79.1 runtime version' and "veritas-max-product-v80.0-unified-execution-core" in dst:
                already.append(label+" (superseded by v80)")
            elif label=='v78.1 system rule arbitration integration' and "trade_integrity_layer(asset,horizon,f,trade_plan,research_dec)" in dst:
                already.append(label+" (superseded by v79)")
            elif label=='v78.0 execution consistency integration' and "system_rule_arbitration(asset,horizon,f,trade_plan,research_dec)" in dst:
                already.append(label+" (superseded by v78.1)")
            elif label=='v77.0 decision quality integration' and "execution_consistency_layer(asset,horizon,f,trade_plan,research_dec)" in dst:
                already.append(label+" (superseded by v78)")
            elif label=='dashboard timeout' and "setTimeout(()=>ctl.abort(),180000)" in dst:
                already.append(label+" (superseded by v76.2)")
            elif label=='dashboard refresh' and "setInterval(load,60000)" in dst:
                already.append(label+" (superseded by v76.2)")
            else:
                raise RuntimeError(f"expected pattern not found: {label}")

        if dst != src:
            TARGET.write_text(dst, encoding="utf-8")

        # v78: patch the separate paper-portfolio module as part of the same atomic bootstrap.
        portfolio_applied=[]; portfolio_already=[]
        if PORTFOLIO_TARGET.exists():
            psrc=PORTFOLIO_TARGET.read_text(encoding="utf-8")
            pdst=psrc
            for old,new,label in PORTFOLIO_PATCHES:
                if old in pdst:
                    pdst=pdst.replace(old,new,1); portfolio_applied.append(label)
                elif new in pdst:
                    portfolio_already.append(label)
                elif label=='v78 portfolio version' and ("veritas-portfolio-v3-v78-impulse" in pdst or "veritas-portfolio-v6-v80-unified-execution" in pdst):
                    portfolio_already.append(label+" (superseded)")
                elif label=='v79 portfolio version' and "veritas-portfolio-v6-v80-unified-execution" in pdst:
                    portfolio_already.append(label+" (superseded)")
                elif label=='v79.1 portfolio version' and "veritas-portfolio-v6-v80-unified-execution" in pdst:
                    portfolio_already.append(label+" (superseded by v80)")
                else:
                    raise RuntimeError(f"portfolio expected pattern not found: {label}")
            if pdst!=psrc:
                PORTFOLIO_TARGET.write_text(pdst,encoding="utf-8")
            applied.extend(portfolio_applied)
            already.extend(portfolio_already)
        else:
            raise RuntimeError("veritas_portfolio.py missing")

        # synchronize the public VERSION label in the auxiliary V70 module.
        try:
            v70p=Path(__file__).resolve().with_name("veritas_v70.py")
            if v70p.exists():
                v70s=v70p.read_text(encoding="utf-8")
                v70n=v70s.replace(
                    'VERSION = "veritas-max-product-v72.0-market-intelligence-causal-decision"',
                    'VERSION = "veritas-max-product-v80.0-unified-execution-core"',
                    1
                ).replace(
                    "VERSION = 'veritas-max-product-v72.0-market-intelligence-causal-decision'",
                    "VERSION = 'veritas-max-product-v80.0-unified-execution-core'",
                    1
                )
                if v70n!=v70s:
                    v70p.write_text(v70n,encoding="utf-8")
                    applied.append("v75.1 V70 version sync")
                elif "veritas-max-product-v80.0-unified-execution-core" in v70s:
                    already.append("v75.1 V70 version sync")
        except Exception as vex:
            print(f"[VERITAS BOOTSTRAP] v75.1 V70 version sync warning: {type(vex).__name__}: {vex}", file=sys.stderr, flush=True)

        print(
            "[VERITAS BOOTSTRAP] v80.0 OK: "
            + "; ".join(applied + [x + " (already)" for x in already]),
            flush=True,
        )
    except Exception as exc:
        print(
            f"[VERITAS BOOTSTRAP] v80.0 skipped safely: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

apply()
