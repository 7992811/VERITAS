"""
VERITAS v72.1 bootstrap patch.

Loaded automatically by Python when repository root is on PYTHONPATH.
Render keeps the existing start command:
    python veritas_intelligence.py

Required env:
    PYTHONPATH=.

Patches only four known source fragments:
- dashboard timeout 12s -> 30s
- dashboard refresh 30s -> 45s
- horizon-aware invalidation for active trades
- closed-trade P&L display: RUB + %
"""
from pathlib import Path
import sys

TARGET = Path(__file__).resolve().with_name("veritas_intelligence.py")

PATCHES = (
    (
        "setTimeout(()=>ctl.abort(),12000)",
        "setTimeout(()=>ctl.abort(),30000)",
        "dashboard timeout",
    ),
    (
        "setInterval(load,30000)",
        "setInterval(load,45000)",
        "dashboard refresh",
    ),
    (
        "        elif str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='market_structure_failed'",
        "        elif str(x.get('v70_thesis_status') or '') in ('BROKEN','INVALIDATED') or str(x.get('v70_gate_class') or '')=='THESIS_VETO': terminal='INVALIDATION'; reason='thesis_failed'\n"
        "        elif h in ('1h','4h') and str(x.get('entry_quality') or '')=='INVALIDATED': terminal='INVALIDATION'; reason='tactical_entry_failed'",
        "horizon-aware invalidation",
    ),
    (
        """Net <b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}</b><br>${t.horizon||'—'}""",
        """Net <b class="${Number(t.net_pnl_rub||0)>=0?'ok':'bad'}">${t.net_pnl_rub==null?'—':rub(t.net_pnl_rub)}${t.return_on_entry_nav==null?'':' · '+(100*Number(t.return_on_entry_nav)).toFixed(2)+'%'}</b><br>${t.horizon||'—'}""",
        "closed trade P&L percent",
    ),
)

def apply():
    try:
        if not TARGET.exists():
            print("[VERITAS BOOTSTRAP] main file missing", file=sys.stderr, flush=True)
            return

        src = TARGET.read_text(encoding="utf-8")
        dst = src
        applied = []
        already = []

        for old, new, label in PATCHES:
            if old in dst:
                dst = dst.replace(old, new, 1)
                applied.append(label)
            elif new in dst:
                already.append(label)
            else:
                raise RuntimeError(f"expected pattern not found: {label}")

        if dst != src:
            TARGET.write_text(dst, encoding="utf-8")

        print(
            "[VERITAS BOOTSTRAP] v72.1 OK: "
            + "; ".join(applied + [x + " (already)" for x in already]),
            flush=True,
        )
    except Exception as exc:
        # UI/trade-management hotfix must not prevent service startup.
        print(
            f"[VERITAS BOOTSTRAP] v72.1 skipped safely: {type(exc).__name__}: {exc}",
            file=sys.stderr,
            flush=True,
        )

apply()
