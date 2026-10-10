"""Verified shadow learning from real Currency broker episodes.

This module never changes orders, portfolio limits, stops, targets or admissions.
It reads the durable production Currency ledger through the existing read model,
stores a compact immutable learning projection, and marks all management policy
ideas SHADOW_ONLY until the normal OOS/promotion stack explicitly promotes them.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math

import veritas_currency_dashboard as DASH

VERSION = "CURRENCY_LIVE_LEARNING_SHADOW_V1"
TABLE = "currency_live_learning_episodes"


def _num(value):
    try:
        n = float(value)
        return n if math.isfinite(n) else None
    except (TypeError, ValueError, OverflowError):
        return None


def _digest(value):
    return hashlib.sha256(json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False,
        default=str, allow_nan=False).encode()).hexdigest()


def classify(trade):
    """Classify only evidence we actually observed; never infer a better fill."""
    t = dict(trade or {})
    p = t.get("payload") if isinstance(t.get("payload"), dict) else {}
    mfe = _num(t.get("mfe_pct"))
    mae = _num(t.get("mae_pct"))
    capture = _num(t.get("capture_ratio"))
    giveback = _num(t.get("live_giveback_pct", t.get("giveback_pct")))
    net = _num(t.get("net_pnl_rub"))
    gross = _num(t.get("gross_pnl_rub"))
    model = p.get("trade_origin") == "MODEL"
    path = p.get("management_evidence_status") == "DURABLE_LIVE_PATH"
    shadow = p.get("currency_live_management_shadow")
    shadow = shadow if isinstance(shadow, dict) else {}
    dynamic_tp = p.get("currency_dynamic_tp_shadow_at_exit")
    dynamic_tp = dynamic_tp if isinstance(dynamic_tp, dict) else {}
    exit_reason = str(p.get("exit_reason") or t.get("exit_reason") or "").upper()

    attrs = []
    if gross is not None and net is not None and gross > 0 >= net:
        attrs.append("COST_DRAG")
    management_candidate = bool(
        model and path and mfe is not None and mfe >= 0.15
        and giveback is not None and giveback >= 0.10
    )
    dynamic_tp_candidate = bool(
        model and path and exit_reason == "STRATEGY_TARGET_REACHED"
        and dynamic_tp.get("same_direction_impulse_active") is True
        and dynamic_tp.get("defer_fixed_take_profit") is True
    )
    candidate = management_candidate or dynamic_tp_candidate
    if management_candidate:
        attrs.append("CURRENCY_MANAGEMENT_SHADOW_CANDIDATE")
    if dynamic_tp_candidate:
        attrs.append("CURRENCY_DYNAMIC_TP_SHADOW_CANDIDATE")
    if model and path and net is not None and net > 0 and capture is not None and capture >= 0.60:
        attrs.append("GOOD_EXECUTION")
    if not attrs:
        attrs.append("OBSERVED_LIVE_OUTCOME" if net is not None else "UNVERIFIED_LIVE_OUTCOME")

    eligible = bool(model and path and net is not None and mfe is not None and mae is not None)
    primary = (
        "COST_DRAG" if "COST_DRAG" in attrs else
        "CURRENCY_DYNAMIC_TP_SHADOW_CANDIDATE" if dynamic_tp_candidate else
        "CURRENCY_MANAGEMENT_SHADOW_CANDIDATE" if management_candidate else
        "GOOD_EXECUTION" if "GOOD_EXECUTION" in attrs else
        attrs[0]
    )
    payload = {
        "version": VERSION,
        "source": "LIVE_BROKER_LEDGER",
        "trade_origin": p.get("trade_origin"),
        "management_evidence_status": p.get("management_evidence_status"),
        "mfe_pct": mfe, "mae_pct": mae, "capture_ratio": capture, "giveback_pct": giveback,
        "management_shadow": shadow,
        "dynamic_tp_shadow_at_exit": dynamic_tp,
        "management_shadow_candidate": management_candidate,
        "dynamic_tp_shadow_candidate": dynamic_tp_candidate,
        "shadow_candidate": candidate,
        "automatic_action": False,
        "promotion_required": ["OOS", "COST_STRESS", "TIME_STABILITY",
                               "REGIME_STABILITY", "SUFFICIENT_SAMPLE"],
    }
    return {
        "trade_id": t.get("trade_id"),
        "asset": t.get("asset"),
        "direction": t.get("direction"),
        "horizon": t.get("horizon"),
        "opened_at": t.get("opened_at"),
        "closed_at": t.get("closed_at"),
        "net_pnl_rub": net,
        "mfe_pct": mfe,
        "mae_pct": mae,
        "capture_ratio": capture,
        "movement_realization_ratio": capture,
        "giveback_pct": giveback,
        "primary_attribution": primary,
        "attributions": attrs,
        "learning_eligible": eligible,
        "shadow_candidate": candidate,
        "payload": payload,
        "evidence_hash": _digest([t.get("trade_id"), t.get("closed_at"), net, mfe, mae, capture, giveback, p]),
    }


def ensure_schema_on(c):
    c.execute(f"""CREATE TABLE IF NOT EXISTS {TABLE}(
      trade_id TEXT PRIMARY KEY,
      asset TEXT NOT NULL,
      direction TEXT NOT NULL,
      horizon TEXT,
      opened_at TIMESTAMPTZ,
      closed_at TIMESTAMPTZ NOT NULL,
      net_pnl_rub DOUBLE PRECISION,
      mfe_pct DOUBLE PRECISION,
      mae_pct DOUBLE PRECISION,
      capture_ratio DOUBLE PRECISION,
      movement_realization_ratio DOUBLE PRECISION,
      giveback_pct DOUBLE PRECISION,
      primary_attribution TEXT NOT NULL,
      attributions JSONB NOT NULL,
      learning_eligible BOOLEAN NOT NULL,
      shadow_candidate BOOLEAN NOT NULL DEFAULT FALSE,
      evidence_hash TEXT NOT NULL,
      payload JSONB NOT NULL,
      updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )""")
    c.execute(f"""CREATE INDEX IF NOT EXISTS currency_live_learning_closed_idx
      ON {TABLE}(closed_at DESC)""")
    return True


def sync_on(c, *, environment="production"):
    """Read verified live episodes and upsert their shadow-learning projection."""
    ensure_schema_on(c)
    try:
        live = DASH.read_live_currency_on(c, environment=environment,
                                          checked_at=datetime.now(timezone.utc))
    except Exception:
        return {"status": "UNAVAILABLE", "synced": 0, "eligible": 0, "shadow_candidates": 0}
    if not isinstance(live, dict) or live.get("bound") is not True:
        return {"status": str((live or {}).get("status") or "NOT_BOUND"),
                "synced": 0, "eligible": 0, "shadow_candidates": 0}

    synced = eligible = candidates = 0
    for trade in live.get("trades") or []:
        row = classify(trade)
        if not row.get("trade_id") or not row.get("closed_at"):
            continue
        c.execute(f"""INSERT INTO {TABLE}(
          trade_id,asset,direction,horizon,opened_at,closed_at,net_pnl_rub,
          mfe_pct,mae_pct,capture_ratio,movement_realization_ratio,giveback_pct,
          primary_attribution,attributions,learning_eligible,shadow_candidate,
          evidence_hash,payload,updated_at)
          VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::jsonb,%s,%s,%s,%s::jsonb,now())
          ON CONFLICT(trade_id) DO UPDATE SET
            asset=EXCLUDED.asset,direction=EXCLUDED.direction,horizon=EXCLUDED.horizon,
            opened_at=EXCLUDED.opened_at,closed_at=EXCLUDED.closed_at,
            net_pnl_rub=EXCLUDED.net_pnl_rub,mfe_pct=EXCLUDED.mfe_pct,mae_pct=EXCLUDED.mae_pct,
            capture_ratio=EXCLUDED.capture_ratio,
            movement_realization_ratio=EXCLUDED.movement_realization_ratio,
            giveback_pct=EXCLUDED.giveback_pct,
            primary_attribution=EXCLUDED.primary_attribution,
            attributions=EXCLUDED.attributions,learning_eligible=EXCLUDED.learning_eligible,
            shadow_candidate=EXCLUDED.shadow_candidate,evidence_hash=EXCLUDED.evidence_hash,
            payload=EXCLUDED.payload,updated_at=now()
          WHERE {TABLE}.evidence_hash IS DISTINCT FROM EXCLUDED.evidence_hash""",
          (row["trade_id"], row["asset"], row["direction"], row["horizon"],
           row["opened_at"], row["closed_at"], row["net_pnl_rub"], row["mfe_pct"],
           row["mae_pct"], row["capture_ratio"], row["movement_realization_ratio"],
           row["giveback_pct"], row["primary_attribution"],
           json.dumps(row["attributions"], ensure_ascii=False),
           row["learning_eligible"], row["shadow_candidate"], row["evidence_hash"],
           json.dumps(row["payload"], ensure_ascii=False)))
        synced += 1
        eligible += int(row["learning_eligible"])
        candidates += int(row["shadow_candidate"])
    return {"status": "OK", "synced": synced, "eligible": eligible,
            "shadow_candidates": candidates, "version": VERSION}
