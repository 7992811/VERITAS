"""R80 final book prepass must not rewrite identical live state."""
from copy import deepcopy
from types import SimpleNamespace
import unittest
from unittest.mock import patch

import veritas_price_source as VPS
import veritas_thesis_guard as VTG
from test_veritas_r80_snapshot_lifetimes import load_r80, quote, CLOCK


class Cursor:
    def __init__(self, rows=None):
        self.rows=list(rows or [])
        self.rowcount=len(self.rows)
    def fetchall(self):
        return deepcopy(self.rows)
    def fetchone(self):
        return deepcopy(self.rows[0]) if self.rows else None


class Database:
    def __init__(self, rows):
        self.rows=deepcopy(rows)
        self.writes=[]
    def execute(self, sql, params=()):
        if sql.startswith('SELECT * FROM paper_positions'):
            return Cursor(self.rows)
        if sql.startswith('UPDATE '):
            self.writes.append((sql,deepcopy(params)))
            return Cursor([])
        raise AssertionError(sql)


class R80NoopElisionTests(unittest.TestCase):
    def row(self, *, price=None, guard=None, status='OK'):
        q=quote('BRENT')
        identity=VPS.identity('BRENT',q)
        return {
            'portfolio_name':'Champion','asset':'BRENT','direction':'LONG',
            'units':1.,'avg_entry_price':100.,'last_price':q['price'] if price is None else price,
            'stop_price':95.,'active_trade_id':'T-BRENT',
            'payload':{
                'price_source_lock':identity,
                'price_source_status':status,
                'ctc_senior_thesis_guard':deepcopy(guard),
            },
        },q

    def run_case(self,row,q,guard):
        db=Database([row])
        candidates={'BRENT':{'asset':'BRENT','research_decision':'LONG'}}
        summary=[{'asset':'BRENT','horizon':'4h','research_decision':'LONG'}]
        seen={}
        def base(c,name,policy,book,prices,ruonia,usdrub,ts,commission,rows):
            seen.update(name=name,book=deepcopy(book),prices=deepcopy(prices),
                        summary=deepcopy(rows),ts=ts,commission=commission)
            return {'status':'DELEGATED'}
        vpg=SimpleNamespace(
            quote_for_position=lambda z,now=None: deepcopy(q),
        )
        vtm=SimpleNamespace(
            owns_position=lambda z:False,
            apply_trailing=lambda *a,**k:None,
            filter_lower_context=lambda z,b,r:(b,r),
        )
        fn=load_r80('_step_one',{'VPG':vpg,'VTM':vtm,'_r80_base_step_one':base})
        with patch.object(VTG,'guard_open_position',
                          return_value=(deepcopy(candidates),deepcopy(summary),deepcopy(guard))):
            result=fn(db,'Champion',{},deepcopy(candidates),{},16.,80.,CLOCK,.0004,deepcopy(summary))
        return db,result,seen

    def test_identical_mark_and_guard_issue_no_write(self):
        guard={'active':True,'held_horizon':'4h','hard_exit_allowed':False}
        row,q=self.row(guard=guard)
        db,result,seen=self.run_case(row,q,guard)
        self.assertEqual(result,{'status':'DELEGATED'})
        self.assertEqual(db.writes,[])
        self.assertEqual(seen['prices']['BRENT'],q['price'])

    def test_changed_mark_only_writes_position_once(self):
        guard={'active':True,'held_horizon':'4h','hard_exit_allowed':False}
        row,q=self.row(price=quote('BRENT')['price']-1.,guard=guard)
        db,_,_=self.run_case(row,q,guard)
        self.assertEqual(len(db.writes),1)
        self.assertTrue(db.writes[0][0].startswith('UPDATE paper_positions SET last_price='))

    def test_changed_guard_mirrors_position_and_trade_only(self):
        old={'active':True,'held_horizon':'4h','hard_exit_allowed':False}
        new={'active':True,'held_horizon':'4h','hard_exit_allowed':False,'opposite_support':1.2}
        row,q=self.row(guard=old)
        db,_,_=self.run_case(row,q,new)
        self.assertEqual(len(db.writes),2)
        self.assertTrue(db.writes[0][0].startswith('UPDATE paper_positions SET payload='))
        self.assertTrue(db.writes[1][0].startswith('UPDATE paper_trades SET payload='))
        self.assertEqual(db.writes[0][1][0],db.writes[1][1][0])

    def test_source_status_change_still_persists_even_when_price_is_same(self):
        guard={'active':False,'held_horizon':'4h','hard_exit_allowed':True}
        row,q=self.row(guard=None,status='PINNED_SOURCE_QUOTE_UNAVAILABLE')
        db,_,_=self.run_case(row,q,guard)
        self.assertEqual(len(db.writes),1)
        self.assertTrue(db.writes[0][0].startswith('UPDATE paper_positions SET last_price='))
        self.assertIn('price_source_status',db.writes[0][1][1])


if __name__ == '__main__':
    unittest.main()
