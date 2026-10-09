from contextlib import contextmanager
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import patch
import inspect
import unittest

import veritas_continuous_learning as C
from test_veritas_continuous_learning import namespace

class Rows:
    def __init__(self, rows): self.rows=list(rows)
    def fetchall(self): return list(self.rows)
    def fetchone(self): return self.rows[0] if self.rows else None

class Cursor:
    def __init__(self,row):
        self.row=row
        self.selects=0
        self.updates=0
    def execute(self,sql,args=()):
        if "FROM learning_forecasts" in sql and "SELECT id,entity_key" in sql:
            self.selects+=1
            return Rows([self.row])
        if "UPDATE learning_forecasts" in sql:
            self.updates+=1
            return Rows([{"id":self.row["id"]}])
        raise AssertionError(sql)

class OutcomeIoTests(unittest.TestCase):
    def row(self):
        now=C.clock()
        return {"id":1,"entity_key":"episode-1","decision_at":now,
                "due_at":now-timedelta(seconds=1),"expires_at":now+timedelta(hours=1),
                "asset":"BTC","horizon":"5m",
                "evidence":{"quote":{"source_identity":{"key":"TEST"}}}}

    def test_quote_io_is_outside_database_transaction(self):
        app=C.ContinuousLearning(namespace(lambda:None)); app.ready=True
        row=self.row(); cursor=Cursor(row); inside={"value":False}
        @contextmanager
        def tx(connect,context):
            self.assertFalse(inside["value"])
            inside["value"]=True
            try:
                yield cursor
            finally:
                inside["value"]=False
        context=SimpleNamespace(check=lambda:None,sql_timeout_ms=2000,remaining_seconds=6.0)
        def quote(forecast,now):
            self.assertFalse(inside["value"],"quote I/O ran while DB transaction was open")
            return {"price":101}
        outcome={"status":"READY","evidence_hash":"h"}
        with patch.object(C,"transaction",tx), \
             patch.object(app,"_quote",side_effect=quote) as quote_call, \
             patch.object(C,"resolve_forecast",return_value=outcome):
            result,_=app.outcomes(context,{})
        self.assertEqual(result["status"],"OK")
        self.assertEqual(result["resolved"],1)
        self.assertEqual(cursor.selects,1)
        self.assertEqual(cursor.updates,1)
        quote_call.assert_called_once()

    def test_outcome_query_no_long_lived_for_update(self):
        source=inspect.getsource(C.ContinuousLearning.outcomes)
        self.assertNotIn("FOR UPDATE",source)
        self.assertIn("WHERE id=%s AND status='PENDING'",source)
        self.assertIn("RETURNING id",source)
        self.assertIn("OUTCOME_BATCH",source)
        self.assertLessEqual(C.OUTCOME_BATCH,8)

if __name__=="__main__":
    unittest.main()
