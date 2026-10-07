"""Isolated benchmark: saved CNY prefixes, not historical executions or profit."""
from copy import deepcopy as standard_copy
from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
from statistics import median
import sys
import time

import veritas_structural_breakout as new
from veritas_data_copy import deepcopy as native_copy
from test_veritas_structural_cny_episodes import episode_raw

base_path=Path(sys.argv[1])
spec=importlib.util.spec_from_file_location('structural_reference_before_latency',base_path)
old=importlib.util.module_from_spec(spec);spec.loader.exec_module(old)
lanes=('1m','5m','1h','4h','1d','3d','7d')
samples=[(0,'2026-10-07T04:00:00Z',12.722),(0,'2026-10-07T04:01:00Z',12.736),
         (0,'2026-10-07T04:01:05Z',12.737),(0,'2026-10-07T04:04:00Z',12.742),
         (0,'2026-10-07T04:05:00Z',12.821),(0,'2026-10-07T04:06:00Z',12.736),
         (1,'2026-10-07T07:14:00Z',12.752),(1,'2026-10-07T07:15:00Z',12.766),
         (1,'2026-10-07T07:15:05Z',12.765)]
inputs=[(i,at,episode_raw(i,at,price)) for i,at,price in samples]
checked=0
states_old={};states_new={}
for index,at,raw in inputs:
    for lane in lanes:
        key=(index,lane)
        a=old.build_context(raw,lane,at,state=states_old.get(key))
        b=new.build_context(raw,lane,at,state=states_new.get(key))
        if a!=b:raise AssertionError(('Full structural result differs',index,at,lane,a.get('reason'),b.get('reason')))
        # Public event mutation must not mutate its state or original input.
        if b.get('event'):
            before=native_copy(b['quote_state'])
            b['event']['target_ladder'][0]['price']+=.001
            assert b['quote_state']==before
        states_old[key]=a.get('quote_state');states_new[key]=b.get('quote_state')
        checked+=1
# Clock/source failures must preserve the same detached diagnostic state.
raw=inputs[1][2]
for lane in lanes:
    previous=states_old[(0,lane)]
    for clock in ('2026-10-07T04:00:00Z','2026-10-08T04:01:00Z'):
        a=old.build_context(raw,lane,clock,state=previous)
        b=new.build_context(raw,lane,clock,state=previous)
        assert a==b,(lane,clock)
        checked+=1

def run(module):
    state={};module._clear_native_facts_cache()
    for index,at,raw in inputs:
        for lane in lanes:
            key=(index,lane)
            out=module.build_context(raw,lane,at,state=state.get(key))
            state[key]=out.get('quote_state')

runs={'before':[],'after':[]}
for n in range(3):
    for label,module in (('before',old),('after',new)) if n%2==0 else (('after',new),('before',old)):
        start=time.perf_counter();run(module);runs[label].append(time.perf_counter()-start)
source={'key':'SYNTHETIC_TEST','contract':'TEST_ONLY'}
value={'bars':[dict(ts=i,open=12.72,low=12.69,close=12.75,high=12.8,
                   volume=100,timeframe='1m',source_identity=source) for i in range(3500)]}
copy_runs={}
for label,fn in (('standard',standard_copy),('native',native_copy)):
    start=time.perf_counter()
    for _ in range(40):result=fn(value)
    copy_runs[label]=time.perf_counter()-start
    assert result==value
    assert result['bars'][0]['source_identity'] is result['bars'][1]['source_identity']
report={'scope':'ISOLATED_PREFIX_EQUIVALENCE_AND_CPU_BENCHMARK_NOT_PRODUCTION_LATENCY',
        'full_outputs_checked':checked,'reference_sha':'f848e869361fae0356e8aac66671e291290ece6f',
        'context_seconds':runs,'context_median_seconds':{key:median(val) for key,val in runs.items()},
        'context_speed_ratio':median(runs['before'])/median(runs['after']),
        'copy_seconds_40x3500_bars':copy_runs,
        'copy_speed_ratio':copy_runs['standard']/copy_runs['native']}
Path('diagnostics/structural-equivalence-benchmark.json').write_text(json.dumps(report,indent=2))
print(json.dumps(report,indent=2))
