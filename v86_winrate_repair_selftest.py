from pathlib import Path
import sys
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
from veritas_v85.routing import winrate_repair_gate

def row(h='4h',setup='TREND',eligible=True,reason='ok',price=100.0,stop=99.5,net_rr=1.8,noise=.003,score=.80,indep=3,src_cal=None):
    r={'horizon':h,'price':price,'confidence':score,'calibrated_probability':src_cal,
       'research_decision':'LONG','trade_plan':{'eligible':eligible,'reason':reason,'setup':setup,'stop_price':stop,
       'net_expected_to_stop_ratio':net_rr,'noise_floor_stop_distance_pct':noise,'trade_integrity':{}},
       'institutional_signal':{'evidence_independence':{'independent_count':indep}},
       'tactical_reversal':{'active':setup=='TACTICAL_REVERSAL','direction':'LONG','setup':setup,'probability':score}}
    return r

g=winrate_repair_gate(row(),'LONG')
assert g['eligible'] and str(g['max_fraction'])=='0.05',g

g=winrate_repair_gate(row(h='1d',setup='TACTICAL_REVERSAL',score=.84,indep=4,stop=99.0,noise=.004),'LONG')
assert not g['eligible'] and g['reason']=='TACTICAL_REVERSAL_WRONG_HORIZON',g

g=winrate_repair_gate(row(stop=99.85,noise=.003),'LONG')
assert not g['eligible'] and g['reason']=='STOP_INSIDE_EXPECTED_NOISE',g

g=winrate_repair_gate(row(net_rr=1.1),'LONG')
assert not g['eligible'] and g['reason']=='NET_EDGE_AFTER_COST_FAIL',g

g=winrate_repair_gate(row(score=.68,indep=4),'LONG')
assert not g['eligible'] and g['reason']=='UNCALIBRATED_SCORE_TOO_WEAK',g

g=winrate_repair_gate(row(score=.80,indep=1),'LONG')
assert not g['eligible'] and g['reason']=='INSUFFICIENT_INDEPENDENT_EVIDENCE',g

# Empirical calibration may pass the evidence-count restriction, but repair mode still caps size at 5%.
g=winrate_repair_gate(row(score=.80,indep=0,src_cal=.72),'LONG')
assert g['eligible'] and str(g['max_fraction'])=='0.05' and g['source']=='EMPIRICAL_CALIBRATION',g

intel=(root/'veritas_intelligence.py').read_text()
assert "horizon in ('1h','4h')" in intel
assert "count>=5 and prob>=0.76 and rr>=1.50" in intel
assert "net_edge_after_cost_too_small" in intel
assert "stop_inside_expected_noise" in intel
app=(root/'veritas_v86/application.py').read_text()
assert "TACTICAL_REVERSAL','RANGE_RETEST_BREAKOUT" not in app
print('V86_WINRATE_REPAIR_SELFTEST_OK')
