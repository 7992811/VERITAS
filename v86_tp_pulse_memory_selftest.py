from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
import sys,time

root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]

import veritas_intelligence as v
from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook
from veritas_v85.domain import Instrument,Signal,Direction
from veritas_v85.risk import RiskLimits
from veritas_v85.quote_guard import GUARD_SUPPORTED_ASSETS
from veritas_v86.application import apply_movement_state_memory
from common import T,quote

# Executable TP is persisted and works even when no new signal is supplied.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'tp.sqlite3')
    spec=Instrument('BTC','RUB','RUB',commission_rate=D('.0005'))
    book=PaperBook(ledger,{'BTC':spec})
    book.create_account('A',1000000,'tp-test')
    limits=RiskLimits(D('2'),D('1'),D('.10'))
    s=Signal('BTC',Direction.LONG,'TP_IDEA','tp-d1','1h',D('98'),D('.05'),
             T,T+timedelta(hours=2),'TREND','TEST',
             entry_probability=D('.72'),probability_source='MODEL_PRIOR_UNCALIBRATED',
             movement_state='MOVE_START',take_profit_price=D('101'))
    q1=quote('100',T,'tp1')
    o=book.process('A','BTC','tp1',T,q1,s,limits,quotes={'BTC':q1})
    assert o['events'] and o['events'][0]['action']=='OPEN',o
    t2=T+timedelta(minutes=1);q2=quote('101.1',t2,'tp2')
    x=book.process('A','BTC','tp2',t2,q2,None,limits,quotes={'BTC':q2})
    assert x['events'] and x['events'][0]['action']=='CLOSE',x
    assert x['events'][0]['reason']=='TAKE_PROFIT',x
    assert D(x['events'][0]['net_episode'])>0,x

# Movement memory promotes repeated MOVE_START rather than oscillating between states.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'mem.sqlite3')
    row={'asset':'BTC','horizon':'1h','price':100.0,'regime':'RANGE_LOW_VOL',
         'movement_genesis':{'state':'MOVE_START','eligible':True,'direction':'LONG',
                             'score':0.70,'target_fraction':0.05}}
    a=apply_movement_state_memory(ledger,[dict(row)],T)[0]
    assert a['movement_genesis']['state']=='MOVE_START',a
    b=apply_movement_state_memory(ledger,[dict(row)],T+timedelta(seconds=30))[0]
    assert b['movement_genesis']['state']=='CONFIRMED',b
    assert abs(float(b['movement_genesis']['target_fraction'])-.10)<1e-12,b

# Brent case copied from the live miss: local break + impulse + efficiency + 4 confirmations
# must seed an early movement even while the slow committee remains NO_TRADE.
f={
 'price':100.31,
 'intraday_structure':{'direction':'LONG','fresh_breakout':False,'breakout_hold':False,
   'relative_volume':0.70,'session_efficiency':0.126,'session_persistence':0.52,
   'atr_5m':1.44,'recent_swing_anchor':99.61,'breakout_level':98.78,
   'invalidation_price':98.52,'false_breakout':True},
 'horizon_structure':{'direction':'NO_TRADE','raw_direction':'SHORT','score':0.16,'z':-0.06,'persistence':0.50},
 'trend_impulse':{'direction':'SHORT','phase':'NONE','sigma_1h':0.0064},
 'impulse_pivot_break':{'active':False,'direction':'NO_TRADE','candidate_direction':'LONG',
   'confirmations':4,'local_volume_ratio':1.114,'local_efficiency':0.637,'z3':0.557,
   'stop_price':99.735,'target_price':100.65,
   'evidence':{'local_resistance_break':True,'positive_impulse':True,'volume_expansion':False,
               'strong_volume':False,'local_path_efficiency':True,'old_short_failed':True}},
 'range_retest_breakout':{},
 'structural_levels':{'support':99.61,'resistance':100.65}
}
mg=v.movement_genesis_engine('BRENT','1h',f,{},'NO_TRADE',{'eligible':False,'reason':'no_direction'})
assert mg['eligible'] is True,mg
assert mg['direction']=='LONG',mg
assert mg['state'] in ('MOVE_START','CONFIRMED'),mg
assert mg.get('micro_impulse_bridge') is True,mg
assert float(mg['net_rr'])>=1.25,mg

# Fast guard covers all execution assets.
assert set(('BTC','ETH','NQ','BRENT','GOLD','MOEX','CNYRUBF')).issubset(set(GUARD_SUPPORTED_ASSETS))

# Optional Stooq secondary is no longer allowed to delay the fast decision loop.
t=time.monotonic()
try:
    v._stooq_latest('CB.F')
except RuntimeError as e:
    assert 'OPTIONAL_SECONDARY_DISABLED_IN_FAST_LOOP' in str(e)
assert time.monotonic()-t<0.2

print('V86_TP_PULSE_MEMORY_SELFTEST_OK')
