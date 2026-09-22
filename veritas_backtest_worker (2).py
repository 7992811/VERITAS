"""
VERITAS v76 Backtest Lab runner.

Runs heavy historical learning outside the production web process:
- bootstrap historical backtest
- heavy learning maintenance
- v75 walk-forward / OOS / VAULT parameter lab
Then exits so memory is fully released.
"""
import os, json, sys, time, traceback

os.environ.setdefault("VERITAS_ROLE", "learning")
os.environ.setdefault("VERITAS_BACKTEST_ENABLED", "1")
os.environ.setdefault("VERITAS_KNOWLEDGE_AUTOMATION", "0")
os.environ.setdefault("VERITAS_FULL_OVERVIEW_ENABLED", "0")

def main():
    started=time.time()
    try:
        import veritas_intelligence as v

        if not getattr(v, "pg_enabled")():
            raise RuntimeError("DATABASE_URL / Postgres is required for the backtest worker")

        out={
            "version":getattr(v,"VERSION",None),
            "started_at":getattr(v,"now")(),
            "backtest":None,
            "heavy_learning":None,
            "parameter_lab":None,
        }

        out["backtest"]=v.run_bootstrap_backtest("separate_v76_backtest_worker")
        out["heavy_learning"]=v.run_heavy_learning_maintenance("separate_v76_backtest_worker")

        if hasattr(v,"v75_asset_parameter_board"):
            out["parameter_lab"]=v.v75_asset_parameter_board()
        else:
            out["parameter_lab"]={"status":"UNAVAILABLE","reason":"v75 parameter lab not loaded"}

        out["finished_at"]=getattr(v,"now")()
        out["duration_seconds"]=round(time.time()-started,3)
        print(json.dumps(out,ensure_ascii=False,default=str),flush=True)
        return 0
    except Exception as ex:
        err={
            "status":"ERROR",
            "error":f"{type(ex).__name__}: {ex}",
            "traceback":traceback.format_exc(),
            "duration_seconds":round(time.time()-started,3),
        }
        print(json.dumps(err,ensure_ascii=False),file=sys.stderr,flush=True)
        return 1

if __name__=="__main__":
    raise SystemExit(main())
