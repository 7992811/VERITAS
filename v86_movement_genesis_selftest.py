from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
import sys

root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]

import veritas_intelligence as v
from veritas_v85.routing import route
from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook
from veritas_v85.domain import Instrument,Signal,Direction
from veritas_v85.risk import RiskLimits
from common import T,quote

# 1) Strong multi-factor impulse becomes ACCELERATION.
f={
 'price':100.0,
 'intraday_structure':{'direction':'LONG','fresh_breakout':True,'breakout_hold':True,
   'relative_volume':2.0,'session_efficiency':0.70,'session_persistence':0.70,
   'atr_5m':0.20,'recent_swing_anchor':99.4,'breakout_level':99.6,
   'invalidation_price':99.3,'false_breakout':False},
 'horizon_structure':{'direction':'LONG','raw_direction':'LONG','score':0.80,'z':1.80,'persistence':0.70},
 'trend_impulse':{'direction':'LONG','phase':'IMPULSE_TREND','sigma_1h':0.0010},
 'impulse_pivot_break':{'active':True,'direction':'LONG','confirmations':6,
   'local_volume_ratio':2.0,'local_efficiency':0.70,'stop_price':99.2,'target_price':103.0},
 'range_retest_breakout':{},
 'structural_levels':{'support':99.4,'resistance':102.5}
}
mg=v.movement_genesis_engine('BTC','1h',f,{},'NO_TRADE',{'eligible':False,'reason':'no_direction'})
assert mg['eligible'] is True,mg
assert mg['state']=='ACCELERATION',mg
assert mg['direction']=='LONG' and mg['target_fraction']==0.25,mg
assert mg['net_rr']>=1.25,mg

# 2) Movement Genesis may route a strong move while slow committee is still NO_TRADE.
row={
 'asset':'BTC','horizon':'1h','research_decision':'NO_TRADE','decision':'NO_TRADE',
 'price':100.0,'confidence':0.10,'score':0.10,'regime':'RANGE_LOW_VOL',
 'source_gate_pass':True,'market_open':True,'clock_gate_pass':True,'execution_eligible':True,
 'trade_plan':{'eligible':False,'reason':'no_direction'},
 'movement_genesis':mg,'institutional_signal':{'evidence_independence':{'independent_count':0}},
 'horizon_structure':f['horizon_structure'],'closed_confirmations':[]
}
rt=route([row],T,lifetime_seconds=120)
sig=rt.signals.get('BTC')
assert sig is not None,rt.trace
assert sig.direction==Direction.LONG
assert sig.setup_family=='MOVEMENT_GENESIS'
assert sig.movement_state=='ACCELERATION'
assert sig.validated_add is True
assert sig.desired_fraction==D('.25'),sig

# 3) The immutable book stages a movement position: 5% -> 10% -> 25%.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'movement.sqlite3')
    spec=Instrument('BTC','RUB','RUB',commission_rate=D('.0005'))
    book=PaperBook(ledger,{'BTC':spec})
    book.create_account('A',1000000,'movement-test')
    limits=RiskLimits(D('2.5'),D('1'),D('.10'))

    def msig(decision,desired,state,validated,stop='98'):
        return Signal('BTC',Direction.LONG,'MOVE_IDEA',decision,'1h',D(stop),D(desired),
                      T,T+timedelta(hours=2),'MOVEMENT_GENESIS','RANGE_LOW_VOL',
                      confirmations=(),validated_add=validated,
                      entry_probability=D('.75'),probability_source='MODEL_PRIOR_UNCALIBRATED',
                      movement_state=state)

    q1=quote('100',T,'g1')
    r1=book.process('A','BTC','g1',T,q1,msig('d1','.25','MOVE_START',False),limits,quotes={'BTC':q1})
    assert r1['events'] and r1['events'][0]['action']=='OPEN',r1
    assert D(r1['events'][0]['fraction'])<=D('.0501'),r1

    t2=T+timedelta(minutes=1); q2=quote('100.4',t2,'g2')
    s2=Signal('BTC',Direction.LONG,'MOVE_IDEA','d2','1h',D('98.4'),D('.10'),
              t2,t2+timedelta(hours=2),'MOVEMENT_GENESIS','RANGE_LOW_VOL',
              confirmations=(),validated_add=True,
              entry_probability=D('.75'),probability_source='MODEL_PRIOR_UNCALIBRATED',
              movement_state='CONFIRMED')
    r2=book.process('A','BTC','g2',t2,q2,s2,limits,quotes={'BTC':q2})
    assert r2['events'] and r2['events'][0]['action']=='ADD',r2

    t3=T+timedelta(minutes=2); q3=quote('100.9',t3,'g3')
    s3=Signal('BTC',Direction.LONG,'MOVE_IDEA','d3','1h',D('98.9'),D('.25'),
              t3,t3+timedelta(hours=2),'MOVEMENT_GENESIS','RANGE_LOW_VOL',
              confirmations=(),validated_add=True,
              entry_probability=D('.75'),probability_source='MODEL_PRIOR_UNCALIBRATED',
              movement_state='ACCELERATION')
    r3=book.process('A','BTC','g3',t3,q3,s3,limits,quotes={'BTC':q3})
    assert r3['events'] and r3['events'][0]['action']=='ADD',r3

    pos=book.positions('A')
    # Profit Harvest 2.0 may also ratchet the stop and increment revision;
    # two validated ADDs are the invariant we need here.
    assert len(pos)==1 and pos[0].revision>=2,pos
    with ledger.read() as c:
        orders=c.execute("SELECT reason FROM v85_orders WHERE account_id='A' ORDER BY at,intent_id").fetchall()
        reasons=[x['reason'] for x in orders]
        assert len(reasons)==3,reasons
        assert any(str(x).startswith('VALIDATED_SCALE_IN:CONFIRMED') for x in reasons),reasons
        assert any(str(x).startswith('VALIDATED_SCALE_IN:ACCELERATION') for x in reasons),reasons

print('V86_MOVEMENT_GENESIS_SELFTEST_OK')
