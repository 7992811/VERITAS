from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal as D
import sys

root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]

from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook
from veritas_v85.domain import Instrument,Signal,Direction
from veritas_v85.risk import RiskLimits
from common import T,quote

def sig(idea,decision,at,price_stop,state='MOVE_START',tp=None,direction=Direction.LONG):
    return Signal('BTC',direction,idea,decision,'1h',D(str(price_stop)),D('.05'),
                  at,at+timedelta(hours=2),'MOVEMENT_GENESIS','RANGE_LOW_VOL',
                  confirmations=(),validated_add=(state in ('CONFIRMED','ACCELERATION')),
                  entry_probability=D('.75'),probability_source='MODEL_PRIOR_UNCALIBRATED',
                  movement_state=state,take_profit_price=D(str(tp)) if tp is not None else None)

limits=RiskLimits(D('2'),D('1'),D('.10'))

# Winner -> immediate same-direction fresh idea is blocked unless truly new information appears.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'reentry.sqlite3')
    spec=Instrument('BTC','RUB','RUB',commission_rate=D('.0005'))
    book=PaperBook(ledger,{'BTC':spec})
    book.create_account('A',1000000,'reentry-test')

    q1=quote('100',T,'r1')
    a=book.process('A','BTC','r1',T,q1,sig('IDEA_A','d1',T,'98',tp='101'),limits,quotes={'BTC':q1})
    assert a['events'] and a['events'][0]['action']=='OPEN',a

    t2=T+timedelta(minutes=1);q2=quote('101.1',t2,'r2')
    b=book.process('A','BTC','r2',t2,q2,None,limits,quotes={'BTC':q2})
    assert b['events'] and b['events'][0]['action']=='CLOSE' and b['events'][0]['reason']=='TAKE_PROFIT',b

    t3=T+timedelta(minutes=2);q3=quote('101.15',t3,'r3')
    c=book.process('A','BTC','r3',t3,q3,sig('IDEA_B','d2',t3,'99.0',state='MOVE_START',tp='103'),limits,quotes={'BTC':q3})
    assert not c['events'],c
    assert c['management_reason']=='REENTRY_COOLDOWN_NO_NEW_INFORMATION',c

    # A genuine acceleration with >0.4% extension beyond the prior exit may re-enter.
    t4=T+timedelta(minutes=3);q4=quote('101.6',t4,'r4')
    d=book.process('A','BTC','r4',t4,q4,sig('IDEA_C','d3',t4,'99.5',state='ACCELERATION',tp='104'),limits,quotes={'BTC':q4})
    assert d['events'] and d['events'][0]['action']=='OPEN',d

# Opposite-direction reversal is not blocked by the same-direction cooldown.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'reverse.sqlite3')
    spec=Instrument('BTC','RUB','RUB',commission_rate=D('.0005'))
    book=PaperBook(ledger,{'BTC':spec})
    book.create_account('A',1000000,'reverse-test')

    q1=quote('100',T,'o1')
    a=book.process('A','BTC','o1',T,q1,sig('I1','x1',T,'98',tp='101'),limits,quotes={'BTC':q1})
    t2=T+timedelta(minutes=1);q2=quote('101.1',t2,'o2')
    b=book.process('A','BTC','o2',t2,q2,None,limits,quotes={'BTC':q2})
    assert b['events'] and b['events'][0]['action']=='CLOSE'

    t3=T+timedelta(minutes=2);q3=quote('101.0',t3,'o3')
    short=Signal('BTC',Direction.SHORT,'I2','x2','1h',D('103'),D('.05'),
                 t3,t3+timedelta(hours=2),'MOVEMENT_GENESIS','RANGE_LOW_VOL',
                 confirmations=(),validated_add=False,entry_probability=D('.75'),
                 probability_source='MODEL_PRIOR_UNCALIBRATED',movement_state='MOVE_START',
                 take_profit_price=D('99'))
    c=book.process('A','BTC','o3',t3,q3,short,limits,quotes={'BTC':q3})
    assert c['events'] and c['events'][0]['action']=='OPEN',c

print('V86_POST_EXIT_REENTRY_SELFTEST_OK')
