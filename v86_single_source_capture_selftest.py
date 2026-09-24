from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
import sys

root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]

from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook
from veritas_v85.routing import quote_from_raw,hard_reason
from veritas_v85.domain import Instrument
from veritas_v85.risk import RiskLimits
from common import T,quote,signal

raw={'price':100.0,'market_open':True,'source_gate_pass':False,
     'v85_quote':{'event_id':'one-src','primary_time':T.isoformat(),'secondary_time':None,
                  'primary_source':'primary-only','secondary_source':None,
                  'source_verified':False,'max_age_seconds':60}}
q=quote_from_raw(raw,Instrument('BTC','RUB','RUB'))
assert q.source_verified is True
assert hard_reason({'source_gate_pass':False,'execution_eligible':True,'market_open':True,
                    'trade_plan':{},'clock_gate_pass':True}) is None

with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'capture.sqlite3')
    book=PaperBook(ledger,{'BTC':Instrument('BTC','RUB','RUB',commission_rate=D('.0005'))})
    book.create_account('A',1000000,'capture-test')
    limits=RiskLimits(D('2'),D('1'),D('.10'))

    q1=quote('100',T,'m1')
    r1=book.process('A','BTC','m1',T,q1,signal(at=T,stop_price=D('95')),limits,quotes={'BTC':q1})
    assert r1['events'] and r1['events'][0]['action']=='OPEN',r1

    t2=T+timedelta(seconds=10); q2=quote('101',t2,'m2')
    r2=book.process('A','BTC','m2',t2,q2,signal(at=t2,stop_price=D('95')),limits,quotes={'BTC':q2})
    assert not r2['events'],r2

    t3=T+timedelta(seconds=20); q3=quote('100.6',t3,'m3')
    r3=book.process('A','BTC','m3',t3,q3,signal(at=t3,stop_price=D('95')),limits,quotes={'BTC':q3})
    assert r3['events'] and r3['events'][0]['action']=='CLOSE',r3
    assert r3['events'][0]['reason']=='PROFIT_PROTECT_GIVEBACK',r3
    assert D(r3['events'][0]['net_episode'])>0,r3

print('V86_SINGLE_SOURCE_CAPTURE_SELFTEST_OK')
