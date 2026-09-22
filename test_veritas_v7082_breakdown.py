import importlib.util, math
from pathlib import Path
P=Path(__file__).with_name('veritas_intelligence.py')
spec=importlib.util.spec_from_file_location('vi',P); vi=importlib.util.module_from_spec(spec); spec.loader.exec_module(vi)

def bars_breakdown():
    # Synthetic 5m sequence: local low 101.20, bounce to 101.80, then high-volume break to 101.00.
    px=[101.30,101.42,101.26,101.20,101.35,101.55,101.72,101.80,101.66,101.54,101.38,101.28,101.05,101.00]
    out=[]
    for i,x in enumerate(px):
        v=100.0 if i<11 else (150.0 if i==11 else 220.0)
        out.append({'ts':i*300,'open':px[i-1] if i else x,'high':x+0.05,'low':x-0.05,'close':x,'volume':v})
    return out

def test_impulse_breakdown_short_with_structural_stop():
    b=bars_breakdown(); closes=[100+i*0.02 for i in range(120)]
    raw={'asset':'BRENT','price':b[-1]['close'],'intraday_bars':b,'closes':closes,'highs':[x+0.15 for x in closes],
         'lows':[x-0.15 for x in closes],'vols':[100]*len(closes)}
    f={'price':b[-1]['close'],'trend_direction':'LONG','entry_quality':'INVALIDATED',
       'trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'},
       'intraday_structure':{'lifecycle':'FAILURE'},
       'structural_levels':{'support':99.5,'resistance':102.0,'sma18':101.4}}
    r=vi.impulse_breakdown_setup('BRENT',raw,f,0.0)
    assert r['active'] is True, r
    assert r['candidate_direction']=='SHORT'
    assert r['local_support'] is not None and abs(r['local_support']-101.15)<0.20, r
    assert r['stop_anchor']>101.70 and r['stop_price']>r['stop_anchor'], r
    assert r['reward_risk']>=1.5

def test_no_break_no_short():
    b=bars_breakdown(); b[-1]=dict(b[-1],close=101.30,low=101.25,high=101.35)
    raw={'asset':'BRENT','price':101.30,'intraday_bars':b,'closes':[100+i*0.02 for i in range(120)],
         'highs':[100.2+i*0.02 for i in range(120)],'lows':[99.8+i*0.02 for i in range(120)],'vols':[100]*120}
    f={'price':101.30,'trend_direction':'LONG','entry_quality':'INVALIDATED','trend_impulse':{'direction':'LONG','entry_quality':'INVALIDATED'},
       'intraday_structure':{'lifecycle':'FAILURE'},'structural_levels':{'support':99.5,'resistance':102.0,'sma18':101.4}}
    r=vi.impulse_breakdown_setup('BRENT',raw,f,0.0)
    assert r['active'] is False, r

def test_version_and_engine_flag():
    assert '70.8.2' in vi.VERSION
