from pathlib import Path
from tempfile import TemporaryDirectory
import json,sys
root=Path(sys.argv[1]).resolve()
sys.path.insert(0,str(root))
from veritas_v85.storage import SQLiteLedger
from veritas_v86.state_restore import restore_pre_durable_state,TARGET

snap=json.loads(TARGET.read_text(encoding='utf-8'))
with TemporaryDirectory() as td:
    ledger=SQLiteLedger(Path(td)/'restore.sqlite3')
    with ledger.transaction() as c:
        for pf in snap['portfolios']:
            name=pf['name']
            c.execute('INSERT INTO v85_accounts VALUES(?,?,?,?)',(name,'1000000','1000000','86.0.0-dev1/'+name))
        for tr in snap['trades']:
            if tr['status']!='OPEN': continue
            pay=json.loads(tr['payload']); pos=dict(pay['position'])
            current_ep='CUR_'+tr['episode_id'][3:]
            curpos=dict(pos);curpos['episode_id']=current_ep
            curpos['opened_at']='2026-09-24T11:28:02+00:00'
            curpay=dict(pay);curpay['position']=curpos;curpay['entry_fee']=pay['entry_fee']
            c.execute("INSERT INTO v85_episodes VALUES(?,?,?,?,?,?,NULL,?,NULL,'OPEN',?,NULL)",
                      (current_ep,tr['account_id'],tr['asset'],tr['idea_id'],tr['policy_version'],
                       curpos['opened_at'],'CUR_EVENT_'+tr['asset'],json.dumps(curpay)))
            c.execute('INSERT INTO v85_positions VALUES(?,?,?,?,?,?,?)',
                      (tr['account_id'],tr['asset'],current_ep,json.dumps(curpos),pay['entry_fee'],pos['entry_price'],'1'))
    r=restore_pre_durable_state(ledger,allow_non_postgres=True)
    assert r['status']=='APPLIED',r
    assert r['replaced_count']==snap['trade_count'],r
    with ledger.read() as c:
        active=[dict(x) for x in c.execute('SELECT account_id,asset,episode_id FROM v85_positions ORDER BY account_id,asset').fetchall()]
        expected=sorted((t['account_id'],t['asset'],t['episode_id']) for t in snap['trades'] if t['status']=='OPEN')
        got=sorted((x['account_id'],x['asset'],x['episode_id']) for x in active)
        assert got==expected,(got,expected)
        dup=c.execute("SELECT COUNT(*) n FROM v85_episodes WHERE status='MIGRATION_DUPLICATE'").fetchone()['n']
        lessons=c.execute('SELECT COUNT(*) n FROM v85_lessons').fetchone()['n']
        closed=c.execute("SELECT COUNT(*) n FROM v85_episodes WHERE status='CLOSED'").fetchone()['n']
        archives=c.execute('SELECT COUNT(*) n FROM v86_state_archive').fetchone()['n']
        assert dup==snap['trade_count']
        assert lessons==0 and closed==0
        assert archives==2
    r2=restore_pre_durable_state(ledger,allow_non_postgres=True)
    assert r2['status']=='ALREADY_APPLIED',r2
print('V86_STATE_RESTORE_SELFTEST_OK')
