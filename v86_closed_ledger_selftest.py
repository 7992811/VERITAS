from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
from decimal import Decimal
import json,sys
root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]
from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook,backfill_closed_trade_ledger
from veritas_v86.closed_history_recovery import recover_verified_historical_closes
from common import SPEC,LIMITS,T,quote,signal

# Atomic live close -> immutable ledger.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'ledger.sqlite3')
    book=PaperBook(ledger,{'BTC':SPEC})
    book.create_account('A',1000000,'v86-ledger-test')
    opened=book.process('A','BTC','m1',T,quote(),signal(),LIMITS)
    ep=opened['events'][0]['episode_id']
    at=T+timedelta(minutes=1)
    closed=book.process('A','BTC','m2',at,quote('94',at,'m2'),None,LIMITS)
    assert closed['events'][0]['action']=='CLOSE'
    with ledger.read() as c:
        rows=c.execute('SELECT * FROM v86_closed_trade_ledger WHERE episode_id=?',(ep,)).fetchall()
        assert len(rows)==1
        row=dict(rows[0])
        assert row['record_kind']=='CLOSED_FINAL'
        assert row['finalization_contract']=='CLOSED_FINAL_V1'
        assert int(row['learning_eligible'])==1
        assert row['record_hash'] and row['net_pnl'] is not None
    r=backfill_closed_trade_ledger(ledger)
    assert r['inserted']==0 and r['existing']>=1,r

# Verified historical recovery -> accounting once, ledger once, never learning.
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'recovery.sqlite3')
    book=PaperBook(ledger,{'BTC':SPEC})
    book.create_account('Impulse',1000000,'86.0.0-dev1/Impulse')
    r=recover_verified_historical_closes(ledger)
    assert r['status']=='APPLIED',r
    expected=Decimal('1000000')+Decimal('-1325.2333010921437500')
    with ledger.read() as c:
        acc=c.execute('SELECT realized_equity FROM v85_accounts WHERE account_id=?',('Impulse',)).fetchone()
        assert Decimal(str(acc['realized_equity']))==expected,(acc['realized_equity'],expected)
        row=c.execute('SELECT * FROM v86_closed_trade_ledger WHERE episode_id=?',
                      ('EP_857bd4c759b192032369062acdfacf1a',)).fetchone()
        assert row and row['record_kind']=='RECOVERED_HISTORICAL_CLOSE'
        assert int(row['learning_eligible'])==0
        assert row['net_pnl']=='-1325.2333010921437500'
        lessons=c.execute('SELECT COUNT(*) n FROM v85_lessons').fetchone()['n']
        assert lessons==0
    r2=recover_verified_historical_closes(ledger)
    assert r2['status']=='ALREADY_APPLIED',r2
    with ledger.read() as c:
        acc2=c.execute('SELECT realized_equity FROM v85_accounts WHERE account_id=?',('Impulse',)).fetchone()
        assert Decimal(str(acc2['realized_equity']))==expected
        n=c.execute('SELECT COUNT(*) n FROM v86_closed_trade_ledger').fetchone()['n']
        assert n==1,n
print('V86_CLOSED_LEDGER_SELFTEST_OK')
