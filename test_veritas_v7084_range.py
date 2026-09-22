import importlib.util, pathlib, sys
ROOT=pathlib.Path(__file__).parent
spec=importlib.util.spec_from_file_location('vi7084', ROOT/'veritas_intelligence.py')
vi=importlib.util.module_from_spec(spec); spec.loader.exec_module(vi)


def _bars():
    # Synthetic MOEX range: resistance around 2300, support around 2285,
    # final bars bounce from support before a prospective upside breakout.
    closes=[2291,2293,2296,2298,2299,2300,2298,2296,2294,2292,2290,2288,2286,2285.2,2285.4,2286.2,2287.0,2287.4,2288.0,2288.6]
    out=[]
    for i,c in enumerate(closes):
        hi=c+0.8; lo=c-0.8
        if i==5: hi=2300.2
        if i==13: lo=2284.8
        vol=100.0 if i<17 else 115.0
        out.append({'ts':i*300,'open':c-0.2,'high':hi,'low':lo,'close':c,'volume':vol})
    return out


def test_moex_retest_opens_participation_long_with_stop_below_support_zone():
    raw={'intraday_5m':_bars()}
    f={'price':2288.6,'trend_impulse':{'direction':'LONG'},'horizon_structure_direction':'LONG',
       'horizon_structure':{'direction':'LONG','score':0.66},
       'intraday_structure':{'score':0.64,'lifecycle':'CONFIRMATION','breakout_hold':True,'volume_confirmed':True,'relative_volume':1.05},
       'structural_levels':{'trend_bias':0.45}}
    inst={'investor_signal':'BUY','evidence_independence':{'independent_count':5}}
    x=vi.range_retest_breakout_setup('MOEX',raw,f,inst)
    assert x['state']=='RETEST_ENTRY', x
    assert x['active'] is True, x
    assert x['direction']=='LONG'
    assert 2284 <= x['support'] <= 2286
    assert 2299 <= x['resistance'] <= 2301
    assert x['stop_price'] < 2280.5, x
    assert x['reward_risk'] >= 1.2, x
    assert x['initial_position_fraction'] in (0.10,0.15)


def test_impulse_break_above_resistance_requests_add():
    b=_bars()
    # append impulsive breakout with stronger volume
    for j,c in enumerate([2294,2298,2301.6]):
        b.append({'ts':(len(b)+j)*300,'open':c-0.5,'high':c+0.6,'low':c-0.6,'close':c,'volume':220.0})
    raw={'intraday_5m':b}
    f={'price':2301.6,'trend_impulse':{'direction':'LONG'},'horizon_structure_direction':'LONG',
       'horizon_structure':{'direction':'LONG','score':0.72},
       'intraday_structure':{'score':0.70,'lifecycle':'CONFIRMATION','breakout_hold':True,'volume_confirmed':True,'relative_volume':1.5},
       'structural_levels':{'trend_bias':0.45}}
    inst={'investor_signal':'BUY','evidence_independence':{'independent_count':5}}
    x=vi.range_retest_breakout_setup('MOEX',raw,f,inst)
    assert x['state']=='BREAKOUT_ADD', x
    assert x['active'] is True
    assert x['add_active'] is True
    assert x['initial_position_fraction'] >= 0.20

specp=importlib.util.spec_from_file_location('vp7084', ROOT/'veritas_portfolio.py')
vp=importlib.util.module_from_spec(specp); specp.loader.exec_module(vp)


def _portfolio_row(state='RETEST_ENTRY', prob=0.78, rr=2.0):
    return {'asset':'MOEX','horizon':'1h','research_decision':'LONG','price':2288.6,
            'source_gate_pass':True,'direct_sources':1,'execution_eligible':False,
            'confidence':0.55,'calibrated_probability':None,
            'range_retest_breakout':{'active':True,'direction':'LONG','state':state,'probability':prob,
                                      'confirmations':5,'reward_risk':rr,'initial_position_fraction':0.15},
            'trade_plan':{'eligible':True,'reason':'range_retest_breakout','expected_to_stop_ratio':rr,
                          'stop_price':2279.7,'initial_position_fraction':0.15},
            'institutional_signal':{'action':'ENTER_CANDIDATE','investor_signal':'BUY','risk_pct':0.004,
                'evidence_independence':{'independent_count':5},'breakout_quality':{'quality_score':0.70}},
            'horizon_structure':{'score':0.65}}


def test_paper_portfolio_can_participate_research_only_but_small():
    row=_portfolio_row()
    best=vp._best_by_asset([row])
    assert 'MOEX' in best, best
    f=vp._desired_fraction(best['MOEX'],vp.POLICIES['Champion'],0.0)
    assert 0.05-1e-9 <= f <= 0.15+1e-9, f


def test_range_setup_does_not_bypass_source_gate_for_unqualified_normal_signal():
    row=_portfolio_row(); row['range_retest_breakout']={'active':False}; row['tactical_reversal']={'active':False}
    assert 'MOEX' not in vp._best_by_asset([row])


def test_breakout_impulse_can_scale_beyond_probe_but_is_capped():
    row=_portfolio_row(state='BREAKOUT_ADD',prob=0.82,rr=1.6)
    best=vp._best_by_asset([row]); f=vp._desired_fraction(best['MOEX'],vp.POLICIES['Champion'],0.0)
    assert 0.20-1e-9 <= f <= 0.30+1e-9, f
