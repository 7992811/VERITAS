from pathlib import Path

core=Path("veritas_intelligence.py")
src=core.read_text(encoding="utf-8")
old="""    if live and len(live.get('portfolios') or [])==4:
        out=dict(live); out['api_source']='live_memory'
        with _v90r25_pf_lock:
            _v90r25_pf_cache.update({'at':time.time(),'value':dict(out)})
        return out
"""
new="""    if live and len(live.get('portfolios') or [])==4 and (any(p.get('positions') for p in live.get('portfolios') or []) or not any(abs(float(p.get('gross_leverage') or ((p.get('latest') or {}).get('gross_leverage') or 0)))>0.002 for p in live.get('portfolios') or [])):
        out=dict(live); out['api_source']='live_memory'
        with _v90r25_pf_lock:
            _v90r25_pf_cache.update({'at':time.time(),'value':dict(out)})
        return out
"""
if old not in src:
    raise SystemExit("portfolio fast-path block not found")
src=src.replace(old,new,1)
core.write_text(src,encoding="utf-8")

test=Path("test_veritas_v90_safety.py")
ts=test.read_text(encoding="utf-8")
marker="class PortfolioApiCompletenessTests(unittest.TestCase):"
if marker not in ts:
    block=r'''

class PortfolioApiCompletenessTests(unittest.TestCase):
    def test_nonzero_exposure_without_position_rows_uses_sql_fallback(self):
        import veritas_intelligence as vi
        old_cycle=vi.last_cycle
        old_enabled=vi.pg_enabled
        old_connect=vi.pg_connect
        old_cache=dict(vi._v90r25_pf_cache)
        try:
            vi.last_cycle={'portfolio_autopilot':{'portfolios':[
                {'name':'Impulse','gross_leverage':0.05},
                {'name':'Aggressive','gross_leverage':0.30},
                {'name':'Champion','gross_leverage':0.0},
                {'name':'Challenger','gross_leverage':0.0},
            ]}}
            vi._v90r25_pf_cache.update({'at':0.0,'value':None})
            vi.pg_enabled=lambda: True
            def sql_fallback_reached():
                raise RuntimeError('SQL_FALLBACK_REACHED')
            vi.pg_connect=sql_fallback_reached
            with self.assertRaisesRegex(RuntimeError,'SQL_FALLBACK_REACHED'):
                vi._v90r25_portfolios_fast()
        finally:
            vi.last_cycle=old_cycle
            vi.pg_enabled=old_enabled
            vi.pg_connect=old_connect
            vi._v90r25_pf_cache.clear()
            vi._v90r25_pf_cache.update(old_cache)

    def test_zero_exposure_can_use_fast_memory_without_positions(self):
        import veritas_intelligence as vi
        old_cycle=vi.last_cycle
        old_cache=dict(vi._v90r25_pf_cache)
        try:
            vi.last_cycle={'portfolio_autopilot':{'portfolios':[
                {'name':'Impulse','gross_leverage':0.0},
                {'name':'Aggressive','gross_leverage':0.0},
                {'name':'Champion','gross_leverage':0.0},
                {'name':'Challenger','gross_leverage':0.0},
            ]}}
            vi._v90r25_pf_cache.update({'at':0.0,'value':None})
            out=vi._v90r25_portfolios_fast()
            self.assertEqual(out.get('api_source'),'live_memory')
        finally:
            vi.last_cycle=old_cycle
            vi._v90r25_pf_cache.clear()
            vi._v90r25_pf_cache.update(old_cache)
'''
    anchor="
if __name__ == '__main__':"
    if anchor in ts:
        ts=ts.replace(anchor,block+anchor,1)
    else:
        ts += block
    test.write_text(ts,encoding="utf-8")

for p in (Path(".github/workflows/veritas-fix-position-api.yml"),Path("tools/fix_position_api_r44.py")):
    if p.exists():
        p.unlink()
print("POSITION_API_R44_PATCHED")
