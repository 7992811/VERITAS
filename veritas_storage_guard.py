from __future__ import annotations

import os
import time
from typing import Any, Dict

VERSION = "veritas-storage-guard-v1"


def install_storage_guard(ns: Dict[str, Any]) -> None:
    """Install bounded-storage wrappers into the already-loaded VERITAS runtime."""
    marker = "_V90_STORAGE_GUARD_INSTALLED"
    if ns.get(marker):
        return
    ns[marker] = True

    soft_bytes = max(
        128 * 1024 * 1024,
        int(os.getenv("VERITAS_STORAGE_SOFT_BYTES", str(650 * 1024 * 1024))),
    )
    hard_bytes = max(
        soft_bytes + 64 * 1024 * 1024,
        int(os.getenv("VERITAS_STORAGE_HARD_BYTES", str(800 * 1024 * 1024))),
    )
    check_seconds = max(60, int(os.getenv("VERITAS_STORAGE_CHECK_SECONDS", "300")))

    state = {"checked_at": 0.0, "bytes": None, "tier": "UNKNOWN", "last_emitted": None}
    retention_state = {"last_at": 0.0}
    amber_drop = {
        "decision",
        "meta_signal",
        "setup_learning",
        "admission_learning",
        "trade_counterfactual_lab",
        "impulse_genesis_learning",
    }
    red_drop = amber_drop | {"profitability_learning"}

    def emit(event: str, **kwargs: Any) -> None:
        fn = ns.get("emit")
        if callable(fn):
            try:
                fn(event, **kwargs)
            except Exception:
                pass

    def database_url() -> str:
        return str(ns.get("DATABASE_URL") or "").strip()

    def storage_guard(force: bool = False) -> Dict[str, Any]:
        now_ts = time.time()
        if (
            not force
            and now_ts - float(state.get("checked_at") or 0.0) < check_seconds
        ):
            return dict(state)

        out = {"checked_at": now_ts, "bytes": None, "tier": "UNKNOWN"}
        psycopg = ns.get("psycopg")
        url = database_url()
        if not url or psycopg is None:
            state.update(out)
            return dict(state)

        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=3) as conn:
                conn.execute("SET statement_timeout='3000ms'")
                row = conn.execute(
                    "SELECT pg_database_size(current_database())::bigint"
                ).fetchone()
                used = int(row[0] if row else 0)
            tier = "RED" if used >= hard_bytes else ("AMBER" if used >= soft_bytes else "GREEN")
            out.update({"bytes": used, "tier": tier})
        except Exception:
            out["tier"] = "UNKNOWN"

        previous = state.get("tier")
        state.update(out)
        if out["tier"] != previous or out["tier"] in ("AMBER", "RED"):
            marker_value = (out["tier"], out.get("bytes"))
            if state.get("last_emitted") != marker_value:
                emit(
                    "v90_storage_quota_guard",
                    tier=out["tier"],
                    database_bytes=out.get("bytes"),
                    soft_bytes=soft_bytes,
                    hard_bytes=hard_bytes,
                )
                state["last_emitted"] = marker_value
        return dict(state)

    base_pg_event = ns.get("pg_event")

    def guarded_pg_event(
        event_type: str,
        entity_key: str,
        payload: Any,
        asset: Any = None,
        horizon: Any = None,
        event_ts: Any = None,
    ) -> Any:
        st = storage_guard()
        et = str(event_type or "")
        if st.get("tier") == "RED" and et in red_drop:
            return True
        if st.get("tier") == "AMBER" and et in amber_drop:
            return True
        return base_pg_event(event_type, entity_key, payload, asset, horizon, event_ts)

    if callable(base_pg_event):
        ns["pg_event"] = guarded_pg_event

    base_snapshot = ns.get("save_product_snapshot")

    def guarded_snapshot() -> Any:
        st = storage_guard()
        if st.get("tier") in ("AMBER", "RED"):
            return None
        return base_snapshot()

    if callable(base_snapshot):
        ns["save_product_snapshot"] = guarded_snapshot

    base_retention = ns.get("_v90r37_storage_retention")

    def guarded_retention() -> Any:
        st = storage_guard(force=True)
        now_ts = time.time()
        if st.get("tier") in ("AMBER", "RED"):
            if now_ts - float(retention_state.get("last_at") or 0.0) < 1800:
                return {
                    "status": "NOT_DUE_QUOTA_GUARD",
                    "tier": st.get("tier"),
                    "database_bytes": st.get("bytes"),
                }
            retention_state["last_at"] = now_ts
            maintenance_state = ns.get("_v90r37_maintenance_state")
            if isinstance(maintenance_state, dict):
                maintenance_state["last"] = 0.0
        return base_retention()

    if callable(base_retention):
        ns["_v90r37_storage_retention"] = guarded_retention

    def startup_space_reclaim() -> Dict[str, Any]:
        st = storage_guard(force=True)
        if st.get("tier") not in ("AMBER", "RED"):
            return {
                "status": "NOT_NEEDED",
                "tier": st.get("tier"),
                "database_bytes": st.get("bytes"),
            }

        psycopg = ns.get("psycopg")
        url = database_url()
        if not url or psycopg is None:
            return {"status": "UNAVAILABLE"}

        reclaimed = []
        try:
            with psycopg.connect(url, autocommit=True, connect_timeout=5) as conn:
                conn.execute("SET statement_timeout='8000ms'")
                for schema in ("veritas_v90", "public"):
                    for table in (
                        "product_snapshots",
                        "model_drift_snapshots",
                        "macro_snapshots",
                        "visitor_sessions",
                    ):
                        reg = f"{schema}.{table}"
                        try:
                            row = conn.execute("SELECT to_regclass(%s)", (reg,)).fetchone()
                            if row and row[0]:
                                conn.execute(
                                    f'TRUNCATE TABLE "{schema}"."{table}" RESTART IDENTITY'
                                )
                                reclaimed.append(reg)
                        except Exception:
                            pass

                for schema, table, col, interval in (
                    ("veritas_v90", "paper_nav_history", "observed_at", "3 days"),
                    ("veritas_v90", "product_alerts", "created_at", "3 days"),
                    ("public", "paper_nav_history", "observed_at", "3 days"),
                    ("public", "product_alerts", "created_at", "3 days"),
                ):
                    try:
                        row = conn.execute(
                            "SELECT to_regclass(%s)", (f"{schema}.{table}",)
                        ).fetchone()
                        if row and row[0]:
                            conn.execute(
                                f'DELETE FROM "{schema}"."{table}" '
                                f'WHERE "{col}" < NOW() - INTERVAL \'{interval}\''
                            )
                    except Exception:
                        pass

            storage_guard(force=True)
            emit(
                "v90_storage_startup_reclaim",
                status="OK",
                truncated=reclaimed,
                database_bytes=state.get("bytes"),
                tier=state.get("tier"),
            )
            return {
                "status": "OK",
                "truncated": reclaimed,
                "database_bytes": state.get("bytes"),
            }
        except Exception as ex:
            emit(
                "v90_storage_startup_reclaim",
                status="ERROR",
                error=f"{type(ex).__name__}: {ex}",
            )
            return {"status": "ERROR", "error": f"{type(ex).__name__}: {ex}"}

    ns["_v90r42_storage_guard"] = storage_guard
    ns["_v90r42_storage_state"] = state
    ns["_v90r42_startup_space_reclaim"] = startup_space_reclaim

    try:
        startup_space_reclaim()
    except Exception:
        pass
