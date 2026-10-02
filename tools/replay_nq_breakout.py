"""Replay the reported NQ incident, with explicit historical-availability limits.

Run from repository root: python tools/replay_nq_breakout.py
This is a case regression, not an independent profitability backtest.
"""
import json
import sys
from datetime import datetime,timezone
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import veritas_trend_entry as T
import veritas_execution as X

data=json.loads((ROOT/'tests/fixtures/nq_20261002_breakout.json').read_text())
rows=[]
for hh,px in [('12:29',30990.),('12:31',31047.25),('12:35',31118.25),('13:22',31205.09)]:
    now=datetime.fromisoformat('2026-10-02T'+hh+':00+00:00')
    context=T.build_context(data['5m'],now,'NQ',data['1m'])
    event=context.get('event') or {}
    row={'asset':'NQ','horizon':'5m','research_decision':'LONG','price':px,
         'trend_entry_context':context,
         'trade_plan':{'entry_price':px,'direction':'LONG','stop_price':30000.,
                       'target_price':32500.,'expected_move_pct':.05}}
    prepared=T.prepare_row(row)
    econ=X.economics_gate('NQ',dict(prepared['trade_plan'],initial_position_fraction=.75,horizon='5m'))
    timing=T.event_gate(row,econ.get('modeled_entry_fill') or px,'LONG',now)
    rows.append({'time_utc':now.isoformat(),'reference_price':px,'event_id':event.get('event_id'),
                 'trigger_level':event.get('trigger_level'),'impulse_origin':event.get('impulse_origin'),
                 'stop_price':event.get('stop_price'),'confirmation':event.get('confirmation'),
                 'timing':timing,'economics':econ,'order_eligible':timing['eligible'] and econ['eligible']})
assert rows[1]['timing']['eligible']
assert not rows[-1]['timing']['eligible']
assert rows[1]['event_id']==rows[-1]['event_id']
assert not any(r['order_eligible'] for r in rows)
result={'case':'NQ 2026-10-02','policy':T.VERSION,
        'method':'Closed candles only; modeled adverse fills and fees; 75% initial notional; nearest known H1/H4 barrier',
        'limitations':['Downloaded historical candles do not prove real-time delivery.',
                       'The public quote/proxy used by the old paper trade is not an exchange execution.',
                       'This single selected case does not establish profitability.',
                       'No order qualifies in this case: the early entry lacks net reward/risk; the later entry is extended.'],
        'snapshots':rows}
path=ROOT/'docs/research/r67_nq_case.json';path.parent.mkdir(parents=True,exist_ok=True)
path.write_text(json.dumps(result,ensure_ascii=False,indent=2)+'\n')
for r in rows:
    print(r['time_utc'],r['reference_price'],'level',r['trigger_level'],
          'timing',r['timing']['reason'],'net_RR',r['economics'].get('expected_to_stop_ratio'),
          'order',r['order_eligible'])
