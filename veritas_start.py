"""
VERITAS v72.1 startup wrapper.

Applies UI and trade-management hotfixes before starting veritas_intelligence.py.
"""
from __future__ import annotations
from pathlib import Path
import runpy
import sys

ROOT = Path(__file__).resolve().parent
TARGET = ROOT / "veritas_intelligence.py"

REPLACEMENTS = (('setTimeout(()=>ctl.abort(),12000)', 'setTimeout(()=>ctl.abort(),30000)', 'dashboard timeout 12s->30s'), ('setInterval(load,30000)', 'setInterval(load,45000)', 'dashboard refresh 30s->45s'), ("        elif str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='market_structure_failed'", "        elif str(x.get('v70_thesis_status') or '') in ('BROKEN','INVALIDATED') or str(x.get('v70_gate_class') or '')=='THESIS_VETO': terminal='INVALIDATION'; reason='thesis_failed'\n        elif h in ('1h','4h') and str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='tactical_entry_failed'", 'horizon-aware invalidation'), ('Net <b class="${Number(t.net_pnl_rub||0)>=0?\'ok\':\'bad\'}">${t.net_pnl_rub==null?\'—\':rub(t.net_pnl_rub)}</b><br>${t.horizon||\'—\'}', 'Net <b class="${Number(t.net_pnl_rub||0)>=0?\'ok\':\'bad\'}">${t.net_pnl_rub==null?\'—\':rub(t.net_pnl_rub)}${t.return_on_entry_nav==null?\'\':\' · \'+(100*Number(t.return_on_entry_nav)).toFixed(2)+\'%\'}</b><br>${t.horizon||\'—\'}', 'recent trades net P&L percent'))

def patch_source() -> None:
    if not TARGET.exists():
        raise FileNotFoundError(f"Missing {TARGET.name}")
    src = TARGET.read_text(encoding="utf-8")
    dst = src
    applied = []
    already = []
    for old, new, label in REPLACEMENTS:
        if old in dst:
            dst = dst.replace(old, new, 1)
            applied.append(label)
        elif new in dst:
            already.append(label)
        else:
            raise RuntimeError(f"Expected source pattern not found for: {label}")
    if dst != src:
        TARGET.write_text(dst, encoding="utf-8")
    labels = applied + [f"{x} (already)" for x in already]
    print("[VERITAS START] v72.1 hotfix OK: " + "; ".join(labels), flush=True)

def main() -> None:
    patch_source()
    sys.argv[0] = str(TARGET)
    runpy.run_path(str(TARGET), run_name="__main__")

if __name__ == "__main__":
    main()
