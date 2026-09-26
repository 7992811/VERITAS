#!/usr/bin/env python3
"""Idempotent patcher for VERITAS v27 -> v70 orchestration layer.

Usage:
  python apply_v70_patch.py veritas_intelligence.py

It edits the file in place and writes a .v27.bak backup once.  The patch is
fail-closed: if any required anchor is missing, no output is written.
"""
from __future__ import annotations

import pathlib
import re
import shutil
import sys

NEW_VERSION = "veritas-max-product-v70.0-market-os"


def replace_once(text: str, old: str, new: str, label: str) -> str:
    n = text.count(old)
    if n != 1:
        raise RuntimeError(f"{label}: expected exactly 1 anchor, found {n}")
    return text.replace(old, new, 1)


def patch(text: str) -> str:
    if NEW_VERSION in text and "def v70_context_snapshot(" in text:
        return text
    if "veritas-max-product-v27.0-autonomous-trade-intelligence" not in text:
        raise RuntimeError("Input is not the expected v27 main file")

    text = replace_once(
        text,
        "VERSION = 'veritas-max-product-v27.0-autonomous-trade-intelligence'",
        "VERSION = 'veritas-max-product-v70.0-market-os'\n"
        "try:\n"
        "    import veritas_signal_core as V70\n"
        "except Exception:\n"
        "    V70 = None",
        "version/import",
    )

    cfg_anchor = "LEARNING_PROGRESS_WINDOW = max(30, min(500, int(os.getenv('VERITAS_LEARNING_PROGRESS_WINDOW','120'))))"
    cfg_insert = cfg_anchor + "\n" + "\n".join([
        "V70_ENABLED = os.getenv('VERITAS_V70_ENABLED','1').lower() not in ('0','false','no','off')",
        "V70_GATE_MODE = os.getenv('VERITAS_V70_GATE_MODE','shadow').strip().lower() or 'shadow'",
        "V70_OVERVIEW_ENABLED = os.getenv('VERITAS_V70_OVERVIEW_ENABLED','1').lower() not in ('0','false','no','off')",
    ])
    text = replace_once(text, cfg_anchor, cfg_insert, "v70 config")

    anchor = "\ndef compute_product_overview():\n"
    block = r'''

def v70_context_snapshot():
    """Build a bounded context from already-computed/native v27 boards.

    No network calls are added by this layer. Heavy boards remain behind their
    existing caches and all failures degrade to an explicit error field.
    """
    if not V70_ENABLED or V70 is None:
        return {'version':VERSION,'status':'disabled_or_module_unavailable'}
    with lock:
        cyc=dict(last_cycle)
        signals=list(cyc.get('summary') or [])
    def safe(name, fn, default=None):
        try: return fn()
        except Exception as ex:
            return {'status':'error','component':name,'error':f'{type(ex).__name__}: {ex}'} if default is None else default
    opp=safe('opportunities',opportunity_board,{'opportunities':[]})
    alloc=safe('portfolio_allocator',lambda: portfolio_allocator(opp.get('opportunities',[])),{})
    prisk=safe('portfolio_risk',lambda: portfolio_tail_risk(alloc),{})
    ctx={
      'signals':signals,
      'learning_progress':safe('learning_progress',learning_progress,{}),
      'validation':safe('validation_stack',validation_stack,{}),
      'contradictions':safe('contradictions',contradiction_board,{}),
      'event_reaction':safe('event_reaction',event_reaction_board,{}),
      'error_attribution':safe('error_attribution',decision_error_attribution_board,{}),
      'agent_consensus':safe('agent_consensus',agent_consensus_board,{}),
      'drift':safe('drift',model_drift_status,{}),
      'calibration_quality':safe('calibration_quality',calibration_quality,{}),
      'ruleboard':safe('ruleboard',ruleboard,{}),
      'opportunities':opp,
      'portfolio_allocator':alloc,
      'portfolio_risk':prisk,
      'portfolio_stress':safe('portfolio_stress',portfolio_stress,{}),
      'correlation_clusters':(alloc.get('correlation_clusters') if isinstance(alloc,dict) else {}) or {},
      'macro':safe('macro',get_macro_context,{}),
      'macro_regime':safe('macro_regime',macro_regime_summary,{}),
      'cross_asset':safe('cross_asset',cross_asset_shadow,{}),
      'events':safe('events',current_event_context,{}),
      'data_quality':safe('data_quality',data_quality_snapshot,{}),
    }
    return ctx


def v70_quality_board():
    if not V70_ENABLED:
        return {'version':VERSION,'status':'disabled'}
    if V70 is None:
        return {'version':VERSION,'status':'module_unavailable'}
    cache=getattr(v70_quality_board,'_cache',None)
    if cache and time.time()-cache[0] < max(60,ANALYTICS_CACHE_SECONDS):
        return cache[1]
    try:
        out=V70.quality_board(v70_context_snapshot())
    except Exception as ex:
        out={'version':VERSION,'status':'error','error':f'{type(ex).__name__}: {ex}'}
    v70_quality_board._cache=(time.time(),out)
    return out


def v70_pretrade_shadow(asset,horizon,research_decision,confidence,calibration,agents,
                        orth_evidence,source_gate,time_gate,f,event_shadow):
    if not V70_ENABLED or V70 is None:
        return {'status':'DISABLED','allow':True,'decision':research_decision,'size_multiplier':1.0}
    try:
        return V70.pretrade_gate({
          'asset':asset,'horizon':horizon,'research_decision':research_decision,'confidence':confidence,
          'calibrated_probability':(calibration or {}).get('probability_correct'),
          'agents':agents,'effective_evidence':(orth_evidence or {}).get('effective_evidence_count',0),
          'source_gate':source_gate,'time_gate':time_gate,'market_open':f.get('market_open',True),
          'trend_impulse':f.get('trend_impulse') or {},'event_score':(event_shadow or {}).get('score',0.0),
        })
    except Exception as ex:
        return {'status':'ERROR_FAIL_OPEN_SHADOW','allow':True,'decision':research_decision,
                'size_multiplier':1.0,'error':f'{type(ex).__name__}: {ex}'}
'''
    text = replace_once(text, anchor, block + anchor, "v70 host functions")

    # Add a shadow pre-trade governance assessment. It is recorded first; enforcement
    # can only be enabled explicitly after OOS non-degradation is demonstrated.
    cycle_anchor = """                if not source_gate or not time_gate or kill:\n                    research_dec='NO_TRADE'\n                research_signal_tier=classify_signal_tier("""
    cycle_insert = """                if not source_gate or not time_gate or kill:\n                    research_dec='NO_TRADE'\n                v70_pretrade=v70_pretrade_shadow(asset,horizon,research_dec,conf,calibration,agents,orth_evidence,source_gate,time_gate,f,event_shadow)\n                if V70_GATE_MODE=='enforce' and research_dec in ('LONG','SHORT') and not v70_pretrade.get('allow',True):\n                    research_dec='NO_TRADE'; size=0.0\n                elif V70_GATE_MODE=='enforce' and research_dec in ('LONG','SHORT'):\n                    size=float(size)*float(v70_pretrade.get('size_multiplier',1.0) or 0.0)\n                research_signal_tier=classify_signal_tier("""
    text = replace_once(text, cycle_anchor, cycle_insert, "pretrade gate")

    # Persist the governance decision in the durable decision payload.
    text = replace_once(
        text,
        "'research_challenger':research_challenger,'event_shadow':event_shadow,\n                               'causal_shadow':causal_shadow,",
        "'research_challenger':research_challenger,'event_shadow':event_shadow,\n                               'v70_pretrade':v70_pretrade,\n                               'causal_shadow':causal_shadow,",
        "sqlite v70 payload",
    )
    text = replace_once(
        text,
        "'derivatives': deriv,'event_shadow':event_shadow,\n                            'causal_shadow':causal_shadow,",
        "'derivatives': deriv,'event_shadow':event_shadow,'v70_pretrade':v70_pretrade,\n                            'causal_shadow':causal_shadow,",
        "postgres v70 payload",
    )

    # Add compact v70 fields to each live signal without embedding the full board.
    z_anchor = """                     'challenger_confidence':round(float(research_challenger.get('confidence') or 0),4),"""
    z_insert = z_anchor + "\n" + """                     'v70_uncertainty':v70_pretrade.get('uncertainty'),'v70_falsification':v70_pretrade.get('falsification_score'),\n                     'v70_gate_status':v70_pretrade.get('status'),'v70_size_multiplier':v70_pretrade.get('size_multiplier'),"""
    text = replace_once(text, z_anchor, z_insert, "summary v70 fields")

    # Expose v70 as a first-class API.
    endpoint_anchor = """            elif self.path.startswith('/api/v1/v27-quality'):\n                self.reply(v27_quality_board())"""
    endpoint_insert = endpoint_anchor + "\n" + """            elif self.path.startswith('/api/v1/v70'):\n                self.reply(v70_quality_board())"""
    text = replace_once(text, endpoint_anchor, endpoint_insert, "v70 endpoint")

    # Add a bounded cached v70 object to overview. This is deliberately optional.
    overview_anchor = """            'independent_experience':independent_experience_summary(),'learning_report':daily_learning_report(),"""
    overview_insert = overview_anchor + "\n" + """            'v70':(v70_quality_board() if V70_OVERVIEW_ENABLED else {'status':'overview_disabled'}),"""
    text = replace_once(text, overview_anchor, overview_insert, "overview v70")

    # Publish the new API in the startup event.
    startup_anchor = """experiments_api='/api/v1/experiments', v27_quality_api='/api/v1/v27-quality',"""
    startup_insert = startup_anchor + " v70_api='/api/v1/v70',"
    text = replace_once(text, startup_anchor, startup_insert, "startup v70 api")

    return text


def main() -> int:
    if len(sys.argv) != 2:
        print("Usage: python apply_v70_patch.py veritas_intelligence.py", file=sys.stderr)
        return 2
    path=pathlib.Path(sys.argv[1])
    original=path.read_text(encoding="utf-8")
    patched=patch(original)
    if patched == original:
        print("Already patched; no changes made.")
        return 0
    # Parse before touching the source file.
    compile(patched, str(path), "exec")
    backup=path.with_suffix(path.suffix + ".v27.bak")
    if not backup.exists(): shutil.copy2(path,backup)
    path.write_text(patched,encoding="utf-8")
    print(f"Patched {path} -> {NEW_VERSION}; backup: {backup}")
    return 0

if __name__ == "__main__":
    raise SystemExit(main())
