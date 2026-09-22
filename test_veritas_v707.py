import importlib.util
from pathlib import Path

ROOT=Path(__file__).parent
spec=importlib.util.spec_from_file_location('vi',ROOT/'veritas_intelligence.py')
vi=importlib.util.module_from_spec(spec); spec.loader.exec_module(vi)
pspec=importlib.util.spec_from_file_location('vp',ROOT/'veritas_portfolio.py')
vp=importlib.util.module_from_spec(pspec); pspec.loader.exec_module(vp)

def test_thresholds():
    assert vp.POLICIES['Champion']['threshold']==0.70
    assert vp.POLICIES['Challenger']['threshold']==0.77

def test_fast_reversal_short_candidate():
    f={'price':99.0,
       'structural_levels':{'support':97.0,'resistance':99.4,'sma18':99.6,'sma50':98.0},
       'intraday_structure':{'lifecycle':'FAILURE','session_efficiency':0.75,'relative_volume':1.4,'recent_swing_anchor':99.3,'session_high':99.4,'session_low':98.7},
       'trend_impulse':{'entry_quality':'INVALIDATED','sigma_1h':0.003}}
    r=vi.tactical_reversal_features('BRENT',f,prev_price=99.6,causal_score=-0.15)
    assert r['candidate_direction']=='SHORT'
    assert r['probability']>=0.70
    assert r['confirmations']>=4
    assert r['reward_risk']>=1.30
    assert r['active'] is True

def test_levels_shape():
    # 70 synthetic daily closes represented as 24 hourly bars (crypto case)
    c=[]; h=[]; l=[]
    for d in range(70):
        for k in range(24):
            x=100+d*0.1+k*0.001
            c.append(x); h.append(x+0.2); l.append(x-0.2)
    raw={'asset':'BTC','price':c[-1],'closes':c,'highs':h,'lows':l}
    z=vi.structural_levels_features(raw)
    assert z['status']=='OK'
    assert z['sma18'] is not None and z['sma50'] is not None
    assert 'support' in z and 'resistance' in z

def test_tactical_size_cap():
    row={'_pwin':0.80,'institutional_signal':{'investor_signal':'WATCH','evidence_independence':{'independent_count':1}},
         'tactical_reversal':{'active':True,'confirmations':5,'reward_risk':1.8,'structural_confirmed':False},
         'trade_plan':{'eligible':True,'expected_to_stop_ratio':1.8}}
    f=vp._desired_fraction(row,vp.POLICIES['Champion'],0.0)
    assert 0 < f <= 0.150001
