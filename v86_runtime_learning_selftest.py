from pathlib import Path
from tempfile import TemporaryDirectory
from datetime import timedelta
import json,sys
root=Path(sys.argv[1]).resolve()
sys.path[:0]=[str(root),str(root/'tests')]
from veritas_v85.storage import SQLiteLedger
from veritas_v85.book import PaperBook
from common import SPEC,LIMITS,T,quote,signal

with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'closed-final.sqlite3')
    book=PaperBook(ledger,{'BTC':SPEC})
    book.create_account('A',1000000,'v86-selftest')
    opened=book.process('A','BTC','m1',T,quote(),signal(),LIMITS)
    ep=opened['events'][0]['episode_id']
    at=T+timedelta(minutes=1)
    closed=book.process('A','BTC','m2',at,quote('94',at,'m2'),None,LIMITS)
    assert closed['events'] and closed['events'][0]['action']=='CLOSE',closed
    with ledger.read() as c:
        e=c.execute('SELECT status,net_pnl,payload FROM v85_episodes WHERE episode_id=?',(ep,)).fetchone()
        p=json.loads(e['payload']); outcome=p['outcome']
        lessons=c.execute('SELECT COUNT(*) n FROM v85_lessons WHERE episode_id=?',(ep,)).fetchone()['n']
        assert e['status']=='CLOSED' and e['net_pnl'] is not None
        assert outcome['outcome_finalized'] is True
        assert outcome['finalization_contract']=='CLOSED_FINAL_V1'
        assert int(outcome['path_points'])>=2
        assert outcome['learning_eligible'] is True
        assert lessons==1,(outcome,lessons)
print('V86_CLOSED_FINAL_LEARNING_SELFTEST_OK')
