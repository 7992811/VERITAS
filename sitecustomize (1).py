"""
VERITAS v72.3 bootstrap patch.

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
            "[VERITAS BOOTSTRAP] v72.3 OK: "
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
