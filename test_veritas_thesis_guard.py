from datetime import datetime, timezone
import unittest
from unittest.mock import MagicMock

import veritas_thesis_guard as TG


class SeniorThesisGuardTests(unittest.TestCase):
    def _db(self,horizon):
        c=MagicMock()
        c.execute.return_value.fetchone.return_value={"horizon":horizon}
        return c

    @staticmethod
    def _row(h,d,hard=False,confidence=.8):
        return {
            "asset":"NQ","horizon":h,"research_decision":d,"confidence":confidence,
            "trade_plan":{"trade_integrity":{
                "hard_invalidation":hard,
                "hard_reasons":["THESIS_INVALIDATION"] if hard else [],
                "status":"HARD_INVALIDATION" if hard else "PASS",
                "entry_permission":"VETO" if hard else "ENTER",
            }},
            "structure_breakout_grid":{h:{
                "exit_signal":hard,"direction":"LONG","state":"EXIT_REVERSAL"
            }},
        }

    def test_3d_long_is_not_closed_by_lower_tf_short_noise(self):
        z={"asset":"NQ","direction":"LONG","active_trade_id":"t3d","payload":{}}
        rows=[
            self._row("1h","SHORT",True,.80),
            self._row("4h","SHORT",True,.78),
            self._row("1d","LONG",False,.60),
            self._row("3d","LONG",False,.77),
            self._row("7d","SHORT",False,.15),
        ]
        candidate=dict(rows[1],_flip_confirmed=True)
        book,guarded,meta=TG.guard_open_position(self._db("3d"),z,{"NQ":candidate},rows)
        self.assertTrue(meta["active"])
        self.assertFalse(meta["hard_exit_allowed"])
        self.assertFalse(book["NQ"]["_flip_confirmed"])
        for row in guarded:
            self.assertFalse((row["trade_plan"]["trade_integrity"] or {}).get("hard_invalidation"))

    def test_4h_long_survives_tied_senior_conflict(self):
        z={"asset":"NQ","direction":"LONG","active_trade_id":"t4h","payload":{}}
        rows=[
            self._row("4h","SHORT",True,.80),
            self._row("1d","LONG",False,.20),
            self._row("3d","LONG",False,.77),
            self._row("7d","SHORT",False,.20),
        ]
        book,guarded,meta=TG.guard_open_position(self._db("4h"),z,{"NQ":dict(rows[0],_flip_confirmed=True)},rows)
        self.assertTrue(meta["active"],meta)
        self.assertFalse(meta["hard_exit_allowed"])
        exact=next(x for x in guarded if x["horizon"]=="4h")
        self.assertFalse(exact["trade_plan"]["trade_integrity"]["hard_invalidation"])
        self.assertEqual(exact["trade_plan"]["trade_integrity"]["entry_permission"],"WAIT_ENTRY")
        self.assertFalse(book["NQ"]["_flip_confirmed"])

    def test_true_senior_reversal_keeps_hard_exit_authority(self):
        z={"asset":"NQ","direction":"LONG","active_trade_id":"t4h","payload":{}}
        rows=[
            self._row("4h","SHORT",True,.90),
            self._row("1d","SHORT",False,.85),
            self._row("3d","SHORT",False,.85),
            self._row("7d","SHORT",False,.80),
        ]
        candidate=dict(rows[0],_flip_confirmed=True)
        book,guarded,meta=TG.guard_open_position(self._db("4h"),z,{"NQ":candidate},rows)
        self.assertFalse(meta["active"],meta)
        self.assertTrue(meta["hard_exit_allowed"])
        self.assertTrue(next(x for x in guarded if x["horizon"]=="4h")["trade_plan"]["trade_integrity"]["hard_invalidation"])
        self.assertTrue(book["NQ"]["_flip_confirmed"])

    def test_stop_is_not_modified(self):
        z={"asset":"NQ","direction":"LONG","active_trade_id":"t3d","stop_price":31336.38,"payload":{}}
        rows=[self._row("3d","LONG",False,.8),self._row("7d","LONG",False,.6)]
        TG.guard_open_position(self._db("3d"),z,{},rows)
        self.assertEqual(z["stop_price"],31336.38)


if __name__=="__main__":
    unittest.main()
